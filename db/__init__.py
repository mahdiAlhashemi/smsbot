"""Async engine + session factory + schema init."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from db.models import Base

engine = create_async_engine(settings.db_url, echo=False)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


__all__ = ["engine", "session_factory", "init_db", "AsyncSession"]
