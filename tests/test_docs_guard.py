import asyncio
import time

from sqlalchemy import insert

from lenapi import Application, Database
from lenapi.models import ADMIN_SESSION_TABLE, AdminUser


def asgi_request(app, path="/", *, headers=()):
    async def run():
        messages = []
        sent_request = False

        async def receive():
            nonlocal sent_request
            if sent_request:
                return {"type": "http.disconnect"}
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": list(headers),
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = next(message for message in messages if message["type"] == "http.response.body")["body"]
        return start["status"], dict(start["headers"]), body

    return asyncio.run(run())


def make_guarded_app(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'docs_guard.sqlite3'}")
    AdminUser.__table__.create(database.engine)
    ADMIN_SESSION_TABLE.create(database.engine)
    app = Application(
        database=database,
        title="Docs Guard Test",
        security_storage="memory",
        safe_guard_docs_url=True,
        docs_cookie_secure=False,
    )

    @app.get("/health")
    def health():
        return {"ok": True}

    return app, database


def test_private_docs_redirect_and_database_session_access(tmp_path):
    app, database = make_guarded_app(tmp_path)
    try:
        with database.engine.begin() as connection:
            admin_id = connection.execute(
                insert(AdminUser.__table__).values(
                    username="docs-admin",
                    email="docs-admin@example.test",
                    password="already-hashed",
                    created_at=time.time(),
                )
            ).inserted_primary_key[0]

        session_id = asyncio.run(app.docs_guard.create_session(str(admin_id)))
        session_cookie = [(b"cookie", f"lenapi_docs_session={session_id}".encode())]

        status, headers, _ = asgi_request(app, "/docs/")
        assert status == 303
        assert headers[b"location"] == b"/"

        status, _, body = asgi_request(app, "/docs/", headers=session_cookie)
        assert status == 200
        assert b"swagger-ui" in body.lower()

        status, headers, body = asgi_request(app, "/openapi.json", headers=session_cookie)
        assert status == 200
        assert b"/health" in body

        status, headers, body = asgi_request(app, "/")
        assert status == 200
        assert b"Administrator access is required" in body
        assert b"lenapi_docs_csrf=" in headers[b"set-cookie"]
    finally:
        database.close()


def test_regular_api_request_does_not_call_docs_session_database(tmp_path, monkeypatch):
    app, database = make_guarded_app(tmp_path)

    async def fail_if_docs_session_is_checked(_session_id):
        raise AssertionError("regular API requests must bypass docs session lookup")

    monkeypatch.setattr(app.docs_guard, "resolve_session", fail_if_docs_session_is_checked)
    try:
        status, _, body = asgi_request(app, "/health")
        assert status == 200
        assert b'"ok":true' in body.replace(b" ", b"")
    finally:
        database.close()
