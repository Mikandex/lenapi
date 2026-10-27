"""SQLAlchemy engine and session setup for lenApi applications."""

from contextlib import asynccontextmanager, contextmanager
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """Own a SQLAlchemy engine and create one sync Session per request.

    SQLAlchemy selects the database dialect from the URL. Install the matching
    DBAPI driver for MySQL or PostgreSQL; SQLite uses Python's built-in driver.
    """

    def __init__(self, url: str = "sqlite:///lenapi.db", *, engine_options: Mapping[str, Any] | None = None) -> None:
        options = {"pool_pre_ping": True, **(engine_options or {})}
        if make_url(url).get_backend_name() == "sqlite":
            connect_args = dict(options.get("connect_args", {}))
            connect_args.setdefault("timeout", 30)
            options["connect_args"] = connect_args
            busy_timeout_ms = max(0, int(float(connect_args["timeout"]) * 1000))
        else:
            busy_timeout_ms = None
        self.engine: Engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def enable_sqlite_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                cursor.close()
        self.session_factory = sessionmaker(bind=self.engine, class_=Session)
        if self.engine.dialect.name == "sqlite":
            # SQLite creates a file on first connection; create it with Database().
            with self.engine.connect():
                pass

    def session(self) -> Session:
        """Create a session for manual use outside request handlers."""
        return self.session_factory()

    @contextmanager
    def transaction(self):
        """Yield a session that commits on success and rolls back on error.

        Use the yielded session for all related SQLAlchemy operations inside the
        block; do not call ``commit`` manually.
        """
        session = self.session_factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    def close(self) -> None:
        """Dispose the engine's connection pool during application shutdown."""
        self.engine.dispose()


class AsyncDatabase:
    """Async SQLAlchemy engine and request-scoped ``AsyncSession`` factory.

    Install the driver for the chosen database, for example ``aiosqlite`` for
    SQLite, ``asyncpg`` for PostgreSQL, or ``asyncmy`` for MySQL.
    """

    def __init__(self, url: str, *, engine_options: Mapping[str, Any] | None = None) -> None:
        options = {"pool_pre_ping": True, **(engine_options or {})}
        if make_url(url).get_backend_name() == "sqlite":
            connect_args = dict(options.get("connect_args", {}))
            connect_args.setdefault("timeout", 30)
            options["connect_args"] = connect_args
        self.engine: AsyncEngine = create_async_engine(url, **options)
        if self.engine.dialect.name == "sqlite":
            busy_timeout_ms = max(0, int(float(connect_args["timeout"]) * 1000))

            @event.listens_for(self.engine.sync_engine, "connect")
            def enable_async_sqlite_pragmas(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                cursor.close()
        self.session_factory = async_sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )

    def session(self) -> AsyncSession:
        """Create an async session for use with ``async with``."""
        return self.session_factory()

    @asynccontextmanager
    async def transaction(self):
        """Yield an AsyncSession; commit on success and roll back on error."""
        async with self.session_factory() as session:
            async with session.begin():
                yield session

    async def close(self) -> None:
        """Dispose the async connection pool during application shutdown."""
        await self.engine.dispose()
