"""Smoke and permission checks for the optional built-in admin console."""

import asyncio
import base64
import json

from lenapi import Application, Database, Model, model
from lenapi.models import Model as ModelBase


class ConsoleRecord(ModelBase):
    __tablename__ = "console_record_test"
    name = model.StringField(max_length=80, nullable=False)
    password = model.StringField(max_length=120, nullable=True)


def asgi_request(app, path, *, method="GET", headers=(), body=b"", query=b""):
    async def run():
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query, "headers": list(headers),
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        payload = next((m["body"] for m in messages if m["type"] == "http.response.body"), b"")
        response_headers = dict(start["headers"])
        cookies = [value for key, value in start["headers"] if key == b"set-cookie"]
        if cookies:
            response_headers[b"set-cookie-all"] = b"\n".join(cookies)
        return start["status"], response_headers, payload

    return asyncio.run(run())


def basic_auth(username, password):
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return (b"authorization", f"Basic {value}".encode())


def test_admin_console_login_model_crud_and_separate_permissions(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'admin.sqlite3'}")
    Model.metadata.create_all(database.engine)
    app = Application(database=database, security_storage="memory", admin_enabled=True,
                      admin_url="/admin/", admin_cookie_secure=False, safe_guard_docs_url=True,
                      docs_cookie_secure=False)
    try:
        app.docs_guard.create_admin("root", "root@example.test", "very-secure-pass", is_superuser=True)
        assert asyncio.run(app.docs_guard.has_superuser()) is True
        # Admin sign-in is stateless HTTP Basic; the browser does not need
        # CSRF/session cookies to operate the console.
        status, headers, body = asgi_request(app, "/admin")
        assert status == 200
        assert b"Admin sign in" in body
        assert b"page-previous" in body and b"page-next" in body
        assert b"document.querySelector('#record-form')?.addEventListener" in body
        assert b"set-cookie" not in headers
        status, headers, _ = asgi_request(
            app, "/admin/api/login", method="POST",
            headers=[(b"content-type", b"application/json")],
            body=json.dumps({"username": "root", "password": "very-secure-pass"}).encode(),
        )
        assert status == 200
        assert b"set-cookie" not in headers
        auth_headers = [basic_auth("root", "very-secure-pass")]

        # Basic credentials on the admin console do not unlock separately
        # guarded Swagger docs.
        status, headers, _ = asgi_request(app, "/docs/", headers=auth_headers)
        assert status == 303 and headers[b"location"] == b"/"

        status, _, body = asgi_request(app, "/admin/api/models", headers=auth_headers)
        assert status == 200
        assert b"console_record_test" in body

        with database.engine.begin() as connection:
            record_id = connection.execute(
                ConsoleRecord.__table__.insert().values(
                    name="Ada", password="must-not-be-exposed"
                )
            ).inserted_primary_key[0]
            connection.execute(ConsoleRecord.__table__.insert(), [
                {"name": "Grace", "password": None},
                {"name": "Alan", "password": None},
            ])

        status, _, body = asgi_request(
            app, "/admin/api/models/console_record_test", method="POST", headers=auth_headers,
            body=json.dumps({"name": "should-not-be-created"}).encode(),
        )
        assert status == 405

        # The dashboard never returns credential-like columns.
        status, _, body = asgi_request(app, "/admin/api/models/console_record_test", headers=auth_headers)
        assert status == 200
        assert b"must-not-be-exposed" not in body
        assert b'"password"' not in body

        status, _, body = asgi_request(
            app, "/admin/api/models/console_record_test", headers=auth_headers,
            query=b"limit=1&offset=0",
        )
        first_page = json.loads(body)
        assert status == 200
        assert len(first_page["rows"]) == 1
        assert first_page["pagination"] == {
            "limit": 1, "offset": 0, "total_count": 3,
            "previous_offset": None, "next_offset": 1,
        }
        status, _, body = asgi_request(
            app, "/admin/api/models/console_record_test", headers=auth_headers,
            query=b"limit=1&offset=1",
        )
        second_page = json.loads(body)
        assert status == 200
        assert len(second_page["rows"]) == 1
        assert second_page["pagination"]["previous_offset"] == 0
        assert second_page["pagination"]["next_offset"] == 2

        status, _, body = asgi_request(
            app, "/admin/api/admins", method="POST", headers=auth_headers,
            body=json.dumps({
                "username": "reader", "email": "reader@example.test",
                "password": "reader-password",
                "permissions": {"console_record_test": {"view": True, "read": True, "write": True}},
            }).encode(),
        )
        assert status == 201
        reader_headers = [basic_auth("reader", "reader-password")]
        status, _, _ = asgi_request(
            app, f"/admin/api/models/console_record_test/{record_id}", method="DELETE",
            headers=reader_headers,
        )
        assert status == 403

        status, _, _ = asgi_request(
            app, f"/admin/api/models/console_record_test/{record_id}", method="DELETE",
            headers=auth_headers,
        )
        assert status == 200
    finally:
        database.close()


def test_monitoring_requires_admin_and_superuser(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'admin-monitor.sqlite3'}")
    Model.metadata.create_all(database.engine)
    try:
        try:
            Application(database=database, admin_monitoring_enabled=True)
        except ValueError as exc:
            assert "requires ADMIN_ENABLED" in str(exc)
        else:
            raise AssertionError("Monitoring must not enable independently of the admin console")

        app = Application(database=database, security_storage="memory", admin_enabled=True,
                          admin_monitoring_enabled=False)
        app.docs_guard.create_admin("root", "root@example.test", "very-secure-pass", is_superuser=True)
        status, _, _ = asgi_request(
            app, "/admin/api/monitor",
            headers=[basic_auth("root", "very-secure-pass")],
        )
        assert status == 404

        class Process:
            pid = 42

            def name(self):
                return "lenapi-test"

            def cpu_percent(self, interval=None):
                return 12.5

            def memory_info(self):
                return type("Memory", (), {"rss": 2 * 1048576})()

            def num_threads(self):
                return 3

        fake_psutil = type("Psutil", (), {
            "Process": staticmethod(lambda: Process()),
            "virtual_memory": staticmethod(lambda: type("Memory", (), {"percent": 50.0})()),
            "cpu_count": staticmethod(lambda: 4),
        })
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        monitored_app = Application(database=database, security_storage="memory", admin_enabled=True,
                                    admin_monitoring_enabled=True, admin_cookie_secure=False)
        monitored_app.docs_guard.create_admin(
            "monitor-root", "monitor-root@example.test", "very-secure-pass", is_superuser=True
        )
        status, _, body = asgi_request(
            monitored_app, "/admin/api/monitor",
            headers=[basic_auth("monitor-root", "very-secure-pass")],
        )
        assert status == 200 and b'"cpu_percent":12.5' in body

        monitored_app.docs_guard.create_admin(
            "monitor-reader", "monitor-reader@example.test", "reader-password"
        )
        status, _, _ = asgi_request(
            monitored_app, "/admin/api/monitor",
            headers=[basic_auth("monitor-reader", "reader-password")],
        )
        assert status == 403
    finally:
        database.close()
