from sqlalchemy.ext.asyncio import create_async_engine

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine, AsyncAttrs


from app.core.config import settings as st


class Base(AsyncAttrs, DeclarativeBase):
    pass


def create_pg_engine() -> AsyncEngine:
    engine = create_async_engine(
        st.DATABASE_URL,
        pool_size=st.POOL_SIZE,
        max_overflow=st.MAX_OVERFLOW,
        pool_recycle=st.POOL_RECYCLE,
    )
    return engine
