"""Explicit transaction contexts for LenAPI ORM operations."""

from contextlib import asynccontextmanager, contextmanager

from .database import AsyncDatabase, Database
from .models import (
    _current_atomic_session,
    reset_current_atomic_session,
    reset_current_session,
    reset_current_async_session,
    set_current_atomic_session,
    set_current_session,
    set_current_async_session,
)


@contextmanager
def _sync_atomic(database: Database):
    """Run synchronous LenAPI model writes in one commit/rollback unit."""
    active = _current_atomic_session.get()
    if active is not None:
        if active.bind is not database.engine:
            raise RuntimeError("Nested atomic blocks must use the same database")
        with active.begin_nested():
            yield
        return

    # Keep committed model values readable after this short-lived session closes.
    session = database.session_factory(expire_on_commit=False)
    session_token = set_current_session(session)
    atomic_token = set_current_atomic_session(session)
    try:
        with session.begin():
            yield
    finally:
        reset_current_atomic_session(atomic_token)
        reset_current_session(session_token)
        session.close()


@asynccontextmanager
async def _async_atomic(database: AsyncDatabase):
    """Run asynchronous LenAPI model writes in one commit/rollback unit."""
    active = _current_atomic_session.get()
    if active is not None:
        if getattr(active, "bind", None) is not database.engine:
            raise RuntimeError("Nested atomic blocks must use the same database")
        async with active.begin_nested():
            yield
        return

    session = database.session()
    session_token = set_current_async_session(session)
    atomic_token = set_current_atomic_session(session)
    try:
        async with session.begin():
            yield
    finally:
        reset_current_atomic_session(atomic_token)
        reset_current_async_session(session_token)
        await session.close()


class _Transaction:
    """Namespace matching the familiar ``transaction.atomic(database)`` API."""

    @staticmethod
    def atomic(database):
        if isinstance(database, Database):
            return _sync_atomic(database)
        if isinstance(database, AsyncDatabase):
            return _async_atomic(database)
        raise TypeError("transaction.atomic() requires a LenAPI Database or AsyncDatabase")


transaction = _Transaction()
