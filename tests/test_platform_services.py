"""Tests for middleware, database transactions, job queues, and cache services."""

import asyncio
import importlib.util

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lenapi import (
    Application,
    AsyncDatabase,
    BackgroundQueue,
    CORSMiddleware,
    Cache,
    Database,
    RequestIDMiddleware,
    TimingMiddleware,
)
from lenapi.models import Model
from lenapi.model import IntegerField, StringField
from lenapi.models import reset_current_session, set_current_session


class FilterSearchRecord(Model):
    name = StringField(max_length=80)
    category = StringField(max_length=80)
    score = IntegerField()


class UniqueQueryRecord(Model):
    name = StringField(max_length=80)


def test_model_filter_lookups_and_search_any(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'filters.db'}")
    FilterSearchRecord.__table__.create(db.engine)
    session = db.session()
    token = set_current_session(session)
    try:
        FilterSearchRecord.create(name="Ada Lovelace", category="math", score=95)
        FilterSearchRecord.create(name="Grace Hopper", category="computing", score=88)
        FilterSearchRecord.create(name="Alan Turing", category="computing", score=72)

        assert [row.name for row in FilterSearchRecord.filter(score__gte=90).all()] == ["Ada Lovelace"]
        assert [row.name for row in FilterSearchRecord.filter(category__in=["math", "computing"]).order("-score").all()] == [
            "Ada Lovelace", "Grace Hopper", "Alan Turing"
        ]
        assert [row.name for row in FilterSearchRecord.filter(name__icontains="hopper").all()] == ["Grace Hopper"]
        assert {row.name for row in FilterSearchRecord.search_any("computing", "name", "category").all()} == {
            "Grace Hopper", "Alan Turing"
        }
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def test_single_model_query_exists_and_get(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'single-query.db'}")
    UniqueQueryRecord.__table__.create(db.engine)
    session = db.session()
    token = set_current_session(session)
    try:
        UniqueQueryRecord.create(name="Ada")
        assert UniqueQueryRecord.get(name="Ada").exists() is True
        assert UniqueQueryRecord.get(name="Missing").exists() is False
        assert UniqueQueryRecord.get(name="Ada").get().name == "Ada"
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def request(app, path="/", *, method="GET", headers=()):
    async def run():
        messages = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": list(headers),
            "client": ("127.0.0.1", 1111), "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        return messages[0]["status"], dict(messages[0].get("headers", [])), messages

    return asyncio.run(run())


def test_request_id_and_timing_middleware_add_response_headers():
    app = Application(security_storage="memory")
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(TimingMiddleware)

    @app.get("/")
    def home():
        return {"ok": True}

    status, headers, _messages = request(app)
    assert status == 200
    assert headers[b"x-request-id"]
    assert float(headers[b"x-process-time-ms"]) >= 0


def test_request_id_middleware_preserves_a_safe_incoming_id():
    app = Application(security_storage="memory")
    app.add_middleware(RequestIDMiddleware)

    @app.get("/")
    def home():
        return {"ok": True}

    _status, headers, _messages = request(app, headers=[(b"x-request-id", b"trace-123")])
    assert headers[b"x-request-id"] == b"trace-123"


def test_cors_middleware_handles_preflight_and_allowed_origins():
    app = Application(security_storage="memory")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://client.example"],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    @app.get("/data")
    def data():
        return {"ok": True}

    status, headers, _messages = request(
        app, "/data", method="OPTIONS", headers=[
            (b"origin", b"https://client.example"),
            (b"access-control-request-method", b"POST"),
            (b"access-control-request-headers", b"content-type"),
        ],
    )
    assert status == 204
    assert headers[b"access-control-allow-origin"] == b"https://client.example"
    assert b"POST" in headers[b"access-control-allow-methods"]

    status, headers, _messages = request(
        app, "/data", headers=[(b"origin", b"https://client.example")]
    )
    assert status == 200
    assert headers[b"access-control-allow-origin"] == b"https://client.example"


def test_sync_database_transaction_commits_or_rolls_back(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'sync-transactions.db'}")
    try:
        with db.engine.begin() as connection:
            connection.execute(text("CREATE TABLE sample (value INTEGER)"))
        with db.transaction() as session:
            session.execute(text("INSERT INTO sample (value) VALUES (1)"))
        with pytest.raises(RuntimeError):
            with db.transaction() as session:
                session.execute(text("INSERT INTO sample (value) VALUES (2)"))
                raise RuntimeError("force rollback")
        with db.engine.connect() as connection:
            assert connection.execute(text("SELECT value FROM sample")).scalars().all() == [1]
    finally:
        db.close()


def test_async_database_transaction_commits_or_rolls_back(tmp_path):
    async def run():
        db = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'async-transactions.db'}")
        try:
            async with db.engine.begin() as connection:
                await connection.execute(text("CREATE TABLE sample (value INTEGER)"))
            async with db.transaction() as session:
                assert isinstance(session, AsyncSession)
                await session.execute(text("INSERT INTO sample (value) VALUES (1)"))
            with pytest.raises(RuntimeError):
                async with db.transaction() as session:
                    await session.execute(text("INSERT INTO sample (value) VALUES (2)"))
                    raise RuntimeError("force rollback")
            async with db.engine.connect() as connection:
                values = (await connection.execute(text("SELECT value FROM sample"))).scalars().all()
                assert values == [1]
        finally:
            await db.close()

    asyncio.run(run())


def test_background_queue_persists_and_executes_registered_job(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'background-jobs.db'}")
    Model.metadata.create_all(db.engine)
    queue = BackgroundQueue(db, retry_delay=0)
    executed = []

    @queue.task("capture")
    def capture(value, *, label):
        executed.append((value, label))

    async def run():
        job_id = await queue.enqueue("capture", 7, label="job")
        assert await queue.run_once() is True
        status = await queue.status(job_id)
        assert status["status"] == "done"
        assert status["attempts"] == 1
        assert executed == [(7, "job")]
        assert await queue.run_once() is False

    try:
        asyncio.run(run())
    finally:
        db.close()


def test_background_queue_retries_then_marks_job_failed(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'background-retries.db'}")
    Model.metadata.create_all(db.engine)
    queue = BackgroundQueue(db, max_attempts=1, retry_delay=0)

    @queue.task("fail")
    def fail():
        raise RuntimeError("expected failure")

    async def run():
        job_id = await queue.enqueue("fail")
        assert await queue.run_once() is True
        status = await queue.status(job_id)
        assert status["status"] == "failed"
        assert "expected failure" in status["last_error"]

    try:
        asyncio.run(run())
    finally:
        db.close()


def test_background_queue_retries_transient_failure(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'background-retry-success.db'}")
    Model.metadata.create_all(db.engine)
    queue = BackgroundQueue(db, max_attempts=2, retry_delay=0)
    calls = []

    @queue.task("retry")
    def retry():
        calls.append("attempt")
        if len(calls) == 1:
            raise RuntimeError("temporary")

    async def run():
        job_id = await queue.enqueue("retry")
        assert await queue.run_once() is True
        assert await queue.run_once() is True
        status = await queue.status(job_id)
        assert status["status"] == "done"
        assert status["attempts"] == 2
        assert len(calls) == 2

    try:
        asyncio.run(run())
    finally:
        db.close()


def test_redis_job_queue_reports_missing_optional_dependency():
    from lenapi import BackgroundQueue

    if importlib.util.find_spec("redis") is None:
        with pytest.raises(ImportError, match=r"lenapi\[redis\]"):
            BackgroundQueue(backend="redis", redis_url="redis://127.0.0.1:6379/0")


def test_memory_cache_round_trips_expires_and_deletes_values():
    cache = Cache()

    async def run():
        await cache.set("person", {"name": "Ifeanyi"})
        assert await cache.get("person") == {"name": "Ifeanyi"}
        await cache.set("short", [1, 2], ttl=0.01)
        await asyncio.sleep(0.02)
        assert await cache.get("short") is None
        await cache.delete("person")
        assert await cache.get("person", "missing") == "missing"

    asyncio.run(run())


def test_database_cache_uses_shared_security_table(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'cache.db'}")
    Model.metadata.create_all(db.engine)
    cache = Cache("database", engine=db.engine)

    async def run():
        await cache.set("student:1", {"name": "Ada"}, ttl=60)
        assert await cache.get("student:1") == {"name": "Ada"}
        await cache.delete("student:1")
        assert await cache.get("student:1") is None

    try:
        asyncio.run(run())
    finally:
        db.close()
