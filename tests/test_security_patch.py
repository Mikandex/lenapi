import asyncio
from urllib.parse import urlencode

from lenapi import Application, Database, WebSocketService


class FakeWebSocket:
    def __init__(self, *, headers=None, path="/ws/chat"):
        self._headers = headers or {}
        self.scope = {"path": path, "query_string": b""}
        self.path_params = {}
        self.query_string = ""
        self.events = []

    @property
    def headers(self):
        return self._headers

    async def close(self, code=1000, reason=None):
        self.events.append(("close", code, reason))

    async def accept(self, subprotocol=None):
        self.events.append(("accept", subprotocol))


def test_websocket_allowlist_rejects_missing_origin():
    service = WebSocketService(path="/ws/chat", allowed_origins=["https://app.example"])
    socket = FakeWebSocket()

    asyncio.run(service._handle_connection(socket))

    assert socket.events[0][0:2] == ("close", 4403)


def test_websocket_allowlist_accepts_configured_origin():
    service = WebSocketService(path="/ws/chat", allowed_origins=["https://app.example"])
    socket = FakeWebSocket(headers={"origin": "https://app.example"})

    async def authenticated(_websocket):
        return {"id": 1}

    service._authenticate = authenticated
    # A deliberate handler exception is avoided by replacing relay receive with a disconnect.
    from lenapi.service import WebSocketDisconnect

    async def disconnect(_websocket):
        raise WebSocketDisconnect()

    service._receive_message = disconnect
    asyncio.run(service._handle_connection(socket))

    assert any(event[0] == "accept" for event in socket.events)


def test_websocket_total_connection_limit_is_enforced_across_rooms():
    service = WebSocketService(path="/ws/chat", max_total_connections=1)
    service._rooms["occupied-room"] = {object()}
    socket = FakeWebSocket()

    async def authenticated(_websocket):
        return {"id": 1}

    service._authenticate = authenticated
    asyncio.run(service._handle_connection(socket))

    assert socket.events[0][0:2] == ("close", 1013)
    assert "default" not in service._rooms


def test_docs_login_attempts_are_throttled_and_reset_after_success():
    database = Database("sqlite://")
    app = Application(
        database=database,
        safe_guard_docs_url=True,
        docs_cookie_secure=False,
        docs_login_max_attempts=2,
        docs_login_window_seconds=30,
        security_storage="memory",
    )
    try:
        assert app._claim_docs_login("192.0.2.20") == 0
        assert app._claim_docs_login("192.0.2.20") == 0
        assert app._claim_docs_login("192.0.2.20") > 0
        assert app._claim_docs_login("192.0.2.21") == 0
        app._clear_docs_login_attempts("192.0.2.20")
        assert app._claim_docs_login("192.0.2.20") == 0
    finally:
        database.close()


def test_docs_login_throttle_configuration_must_be_positive():
    try:
        Application(docs_login_max_attempts=0)
    except ValueError as exc:
        assert "docs_login_max_attempts" in str(exc)
    else:
        raise AssertionError("zero attempts should be rejected")


def test_docs_login_endpoint_returns_429_after_configured_failed_attempts(monkeypatch):
    database = Database("sqlite://")
    app = Application(
        database=database,
        safe_guard_docs_url=True,
        docs_cookie_secure=False,
        docs_login_max_attempts=1,
        docs_login_window_seconds=30,
        security_storage="memory",
    )

    async def invalid_credentials(_username, _password):
        return None

    monkeypatch.setattr(app.docs_guard, "authenticate", invalid_credentials)

    def request():
        token = "csrf-test-token"
        body = urlencode({"csrf_token": token, "username": "admin", "password": "wrong"}).encode()
        messages = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        async def call():
            await app(
                {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": "/", "raw_path": b"/",
                    "query_string": b"", "client": ("192.0.2.44", 443),
                    "headers": [
                        (b"content-type", b"application/x-www-form-urlencoded"),
                        (b"cookie", f"lenapi_docs_csrf={token}".encode()),
                    ],
                },
                receive,
                send,
            )

        asyncio.run(call())
        start = next(message for message in messages if message["type"] == "http.response.start")
        return start["status"], dict(start["headers"])

    try:
        assert request()[0] == 401
        status, headers = request()
        assert status == 429
        assert int(headers[b"retry-after"]) > 0
    finally:
        database.close()
