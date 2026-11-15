"""Restricted-role RLS checks against a disposable, migrated PostGIS database.

Set RLS_TEST_OWNER_DATABASE_URL and RLS_TEST_APP_DATABASE_URL to async SQLAlchemy
URLs. The app role must already have the production grants. Never point this test
at a database containing user data: it creates and deletes fixture rows.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.common.exceptions import ForbiddenException
from app.repository.registry import DbAccessContext, Registry


@pytest.mark.asyncio
async def test_restricted_role_group_isolation_and_pool_reuse() -> None:
    owner_url = os.environ.get("RLS_TEST_OWNER_DATABASE_URL")
    app_url = os.environ.get("RLS_TEST_APP_DATABASE_URL")
    if not owner_url or not app_url:
        pytest.skip("Requires disposable RLS_TEST_OWNER_DATABASE_URL and RLS_TEST_APP_DATABASE_URL")

    owner = create_async_engine(owner_url)
    app = create_async_engine(app_url, pool_size=1, max_overflow=0)
    u1, u2, g1, g2, g3, m1, m2, t1, t2, r1 = (uuid4() for _ in range(10))
    registry = Registry(app, redis_client=object())

    async def context(connection, actor, group):
        await connection.execute(
            text("SELECT set_config('app.actor_id', :actor, true)"),
            {"actor": str(actor) if actor else ""},
        )
        await connection.execute(
            text("SELECT set_config('app.group_id', :group, true)"),
            {"group": str(group) if group else ""},
        )

    try:
        async with owner.begin() as connection:
            for uid in (u1, u2):
                await connection.execute(
                    text("INSERT INTO users (id, name, email) VALUES (:id, 'RLS test', :email)"),
                    {"id": uid, "email": f"r{uid.hex}@x.test"},
                )
            for gid, uid in ((g1, u1), (g2, u2)):
                await connection.execute(
                    text("INSERT INTO groups (id, name, owner_id) VALUES (:id, :name, :owner)"),
                    {"id": gid, "name": f"RLS {gid}", "owner": uid},
                )
                await connection.execute(
                    text("INSERT INTO group_members (member_id, group_id, role) "
                         "VALUES (:member, :group, 'owner')"),
                    {"member": uid, "group": gid},
                )
            for mid, gid in ((m1, g1), (m2, g2)):
                await connection.execute(
                    text("INSERT INTO maps (id, group_id, name, dimension_x, dimension_y) "
                         "VALUES (:id, :group, :name, 10, 10)"),
                    {"id": mid, "group": gid, "name": f"RLS map {mid}"},
                )
            for tid, gid in ((t1, g1), (t2, g2)):
                await connection.execute(
                    text("INSERT INTO tags (id, group_id, name, color) "
                         "VALUES (:id, :group, :name, '#ffffff')"),
                    {"id": tid, "group": gid, "name": f"RLS tag {tid}"},
                )
            await connection.execute(
                text("INSERT INTO robots (id, group_id, name, serial_num, model, "
                     "connection_status, operational_status) VALUES "
                     "(:id, :group, 'RLS robot', :serial, 'STANDARD', 'OFFLINE', 'IDLE')"),
                {"id": r1, "group": g1, "serial": str(r1)},
            )
            await connection.execute(
                text("INSERT INTO group_members (member_id, group_id, invitation_status, "
                     "invitation_expires_at) VALUES (:member, :group, 'PENDING', "
                     "now() - interval '1 second')"),
                {"member": u2, "group": g1},
            )

        async with app.begin() as connection:
            role = (await connection.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))).one()
            assert role == (False, False)
            assert not await connection.scalar(text(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            ))
            assert not await connection.scalar(text(
                "SELECT has_table_privilege(current_user, 'public.maps', 'TRUNCATE')"
            ))
            assert not await connection.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_class "
                "WHERE relname IN ('groups', 'group_members', 'maps', 'robots', 'tags', "
                "'map_boundaries', 'environment_zones', 'docking_station', "
                "'map_tags', 'robot_tags') AND relowner = current_user::regrole)"
            ))
            assert (await connection.scalar(text("SELECT count(*) FROM maps"))) == 0
            assert (await connection.scalar(text("SELECT count(*) FROM group_members"))) == 0

        async with app.begin() as connection:
            await context(connection, u1, g1)
            assert (await connection.scalar(text("SELECT count(*) FROM maps"))) == 1
            assert (await connection.scalar(
                text("SELECT count(*) FROM maps WHERE id = :id"), {"id": m2}
            )) == 0
            assert (await connection.scalar(text("SELECT count(*) FROM robots"))) == 1
            assert (await connection.scalar(text("SELECT count(*) FROM tags"))) == 1
            assert (await connection.scalar(text("SELECT count(*) FROM groups"))) == 1
            assert (await connection.scalar(text("SELECT count(*) FROM map_tags"))) == 0
            for query in (
                "EXPLAIN SELECT id FROM maps ORDER BY created_at DESC LIMIT 10",
                "EXPLAIN SELECT id FROM robots ORDER BY created_at DESC LIMIT 10",
                "EXPLAIN SELECT id FROM groups ORDER BY name LIMIT 10",
            ):
                assert (await connection.execute(text(query))).first()

        # The pool has only one physical connection; prior context must be gone.
        async with app.begin() as connection:
            assert (await connection.scalar(text("SELECT count(*) FROM maps"))) == 0

        # These narrow definer functions are the only cross-tenant app operations.
        async with app.begin() as connection:
            assert (await connection.scalar(
                text("SELECT public.pleaco_robot_group(:id)"), {"id": r1}
            )) == g1
            assert (await connection.scalar(
                text("SELECT public.pleaco_robot_group(:id)"), {"id": uuid4()}
            )) is None
            assert (await connection.scalar(
                text("SELECT public.pleaco_expire_invitations()")
            )) == 1
        async with owner.begin() as connection:
            assert (await connection.execute(
                text("SELECT invitation_status, invitation_id, invitation_expires_at "
                     "FROM group_members WHERE member_id = :member AND group_id = :group"),
                {"member": u2, "group": g1},
            )).one() == ("REJECTED", None, None)

        async def must_reject(statement: str, params: dict, sqlstate: str) -> None:
            with pytest.raises(DBAPIError) as error:
                async with app.begin() as connection:
                    await context(connection, u1, g1)
                    await connection.execute(text(statement), params)
            assert getattr(error.value.orig, "sqlstate", None) == sqlstate

        await must_reject(
            "INSERT INTO map_tags (map_id, tag_id) VALUES (:map, :tag)",
            {"map": m1, "tag": t2},
            "42501",
        )
        await must_reject(
            "INSERT INTO map_boundaries (id, map_id, geometry) "
            "VALUES (:id, :map, ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 0))",
            {"id": uuid4(), "map": m2},
            "42501",
        )
        await must_reject(
            "UPDATE robots SET map_id = :map WHERE id = :robot",
            {"map": m2, "robot": r1},
            "23503",
        )
        await must_reject(
            "INSERT INTO maps (id, group_id, name, dimension_x, dimension_y) "
            "VALUES (:id, :group, 'Cross-group map', 10, 10)",
            {"id": uuid4(), "group": g2},
            "42501",
        )

        async def count_maps(session):
            return await session.scalar(text("SELECT count(*) FROM maps"))

        assert await registry.transaction_wrapper(count_maps, DbAccessContext(u1, g1)) == 1
        assert await registry.transaction_wrapper(count_maps, DbAccessContext(None, None)) == 0

        # Group creation starts with actor only, then adopts the newly created
        # group context before writing its first membership.
        async with app.begin() as connection:
            await context(connection, u1, None)
            await connection.execute(
                text("INSERT INTO groups (id, name, owner_id) "
                     "VALUES (:id, 'RLS newly created group', :owner)"),
                {"id": g3, "owner": u1},
            )
            await context(connection, u1, g3)
            await connection.execute(
                text("INSERT INTO group_members (member_id, group_id, role) "
                     "VALUES (:member, :group, 'owner')"),
                {"member": u1, "group": g3},
            )
        assert await registry.transaction_wrapper(count_maps, DbAccessContext(u1, g3)) == 0

        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM group_members WHERE member_id = :member AND group_id = :group"),
                {"member": u1, "group": g1},
            )
        with pytest.raises(ForbiddenException):
            await registry.transaction_wrapper(count_maps, DbAccessContext(u1, g1))

        async with owner.begin() as connection:
            await connection.execute(
                text("UPDATE robots SET map_id = :map WHERE id = :robot"),
                {"map": m1, "robot": r1},
            )
            await connection.execute(
                text("DELETE FROM maps WHERE id = :map"), {"map": m1}
            )
            assert (await connection.execute(
                text("SELECT group_id, map_id FROM robots WHERE id = :robot"),
                {"robot": r1},
            )).one() == (g1, None)
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM users WHERE id IN (:u1, :u2)"),
                {"u1": u1, "u2": u2},
            )
        await owner.dispose()
        await app.dispose()
