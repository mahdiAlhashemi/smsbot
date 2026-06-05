"""Async engine + session factory + schema init."""
from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from db.models import Base

log = logging.getLogger(__name__)

_is_sqlite = settings.db_url.startswith("sqlite")

# SQLite: allow long waits for the write lock — 5 background pollers write
# concurrently with live handlers, and each money op is its own commit.
_connect_args = {"timeout": 30} if _is_sqlite else {}
engine = create_async_engine(settings.db_url, echo=False, connect_args=_connect_args)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        # WAL: readers never block the writer. busy_timeout: wait for the lock
        # instead of raising 'database is locked' mid money-transition.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


# Columns added by hand after the initial schema (there is no migration
# framework). A DB copied from an older deploy may be missing them; back-fill
# idempotently at startup so the first money op never crashes on a missing column.
_BACKFILL = {
    "users": [
        ("held", "NUMERIC(12,2) DEFAULT 0"),
        ("is_blocked", "BOOLEAN DEFAULT 0"),
        ("full_name", "VARCHAR(255)"),
        ("language", "VARCHAR(8) DEFAULT 'en'"),
        ("total_spent", "NUMERIC(12,2) DEFAULT 0"),
    ],
    "orders": [
        ("kind", "VARCHAR(8) DEFAULT 'sms'"),
        ("chat_id", "BIGINT"),
        ("message_id", "BIGINT"),
        ("hero_released", "BOOLEAN DEFAULT 1"),
        ("service_name", "VARCHAR(128)"),
        ("country_name", "VARCHAR(128)"),
    ],
}


async def _backfill_columns(conn) -> None:  # noqa: ANN001
    for table, cols in _BACKFILL.items():
        res = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in res.fetchall()}
        for name, ddl in cols:
            if name not in existing:
                log.warning("schema backfill: ALTER TABLE %s ADD COLUMN %s", table, name)
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _is_sqlite:
            await _backfill_columns(conn)


__all__ = ["engine", "session_factory", "init_db", "AsyncSession"]
