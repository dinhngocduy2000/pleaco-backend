from sqlalchemy import text

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine, AsyncAttrs


class Base(AsyncAttrs, DeclarativeBase):
    pass


def create_pg_engine() -> AsyncEngine:
    from app.core.config import settings as st

    engine = create_async_engine(
        st.DATABASE_URL,
        pool_size=st.POOL_SIZE,
        max_overflow=st.MAX_OVERFLOW,
        pool_recycle=st.POOL_RECYCLE,
    )
    return engine


async def verify_restricted_runtime_role(engine: AsyncEngine) -> None:
    """Reject a role or schema that cannot enforce tenant row policies."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT r.rolsuper, r.rolbypassrls,
                           EXISTS (
                               SELECT 1 FROM pg_class c
                               JOIN pg_namespace n ON n.oid = c.relnamespace
                               WHERE n.nspname = 'public'
                                 AND c.relname IN (
                                   'groups', 'group_members', 'maps', 'robots',
                                   'tags', 'map_boundaries', 'environment_zones',
                                   'docking_station', 'map_tags', 'robot_tags'
                                 )
                                 AND pg_has_role(r.oid, c.relowner, 'USAGE')
                           ) AS owns_protected_table
                    FROM pg_roles r WHERE r.rolname = current_user
                    """
                )
            )
        ).one()
        unprotected_tables = (
            await connection.execute(
                text(
                    """
                    SELECT protected.name
                    FROM (VALUES
                        ('groups'), ('group_members'), ('maps'), ('robots'),
                        ('tags'), ('map_boundaries'), ('environment_zones'),
                        ('docking_station'), ('map_tags'), ('robot_tags')
                    ) AS protected(name)
                    LEFT JOIN pg_namespace n ON n.nspname = 'public'
                    LEFT JOIN pg_class c ON c.relnamespace = n.oid
                                        AND c.relname = protected.name
                                        AND c.relkind IN ('r', 'p')
                    WHERE c.oid IS NULL
                       OR NOT c.relrowsecurity
                       OR NOT c.relforcerowsecurity
                    """
                )
            )
        ).scalars().all()
    if any(row):
        raise RuntimeError("Database runtime role can bypass tenant row security")
    if unprotected_tables:
        raise RuntimeError(
            "Tenant row security is not enabled and forced on: "
            + ", ".join(sorted(unprotected_tables))
        )
