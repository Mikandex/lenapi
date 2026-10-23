import asyncio
import importlib
import json
import threading
import time
from urllib.parse import urlencode

import pytest

from lenapi import (
    APIKeyAuth,
    Application,
    BasicAuth,
    Database,
    Depends,
    HMACAuth,
    JWTAuth,
    OpaqueBearerAuth,
    SessionAuth,
    WebSocketService,
    hmac_request_signature,
)
from lenapi.middleware import DocsAuthMiddleware
from lenapi.models import SECURITY_STORE_TABLE


async def _http_request(app, path, *, method="GET", payload=None, headers=()):
    body = b"" if payload is None else json.dumps(payload).encode()
    headers = list(headers)
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "client": ("127.0.0.1", 43210), "server": ("test", 80),
        "headers": headers,
    }, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = next(item for item in messages if item["type"] == "http.response.body")["body"]
    return start["status"], json.loads(response_body or b"null")


def test_authentication_public_api_is_available_from_security_module():
    from lenapi.security import (
        SocialAuth,
        UserPublic,
        UserRegistration,
        api_key,
        authentication,
        authentication_type,
        basic,
        configure,
        hmac,
        install_default_authentication,
        jwt_auth,
        oidc,
        opaque_bearer,
        register_user,
        session,
        webhook,
    )

    authentication_module = importlib.import_module("lenapi.authentication")
    social_auth_module = importlib.import_module("lenapi.auth")
    assert SocialAuth is social_auth_module.SocialAuth
    for exported_name in (
        "UserPublic", "UserRegistration", "api_key", "authentication",
        "authentication_type", "basic", "configure", "hmac",
        "install_default_authentication", "jwt_auth", "oidc",
        "opaque_bearer", "register_user", "session", "webhook",
    ):
        assert locals()[exported_name] is getattr(authentication_module, exported_name)


def test_top_level_auth_imports_remain_compatible():
    from lenapi import SocialAuth, authentication, hmac_auth
    from lenapi.security import SocialAuth as SecuritySocialAuth
    from lenapi.security import authentication as security_authentication
    from lenapi.security import hmac_auth as security_hmac_auth

    assert SocialAuth is SecuritySocialAuth
    assert authentication is security_authentication
    assert hmac_auth is security_hmac_auth


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


def test_docs_login_attempts_use_shared_security_store_across_apps(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'security_store.sqlite3'}")
    SECURITY_STORE_TABLE.create(database.engine)
    apps = []
    for _ in range(2):
        app = Application(
            database=database,
            safe_guard_docs_url=True,
            docs_login_max_attempts=2,
            docs_login_window_seconds=30,
            security_storage="database",
        )
        apps.append(app)

    try:
        assert asyncio.run(apps[0]._claim_docs_login_shared("192.0.2.55")) == 0
        assert asyncio.run(apps[1]._claim_docs_login_shared("192.0.2.55")) == 0
        assert asyncio.run(apps[0]._claim_docs_login_shared("192.0.2.55")) > 0
        asyncio.run(apps[1]._clear_docs_login_attempts_shared("192.0.2.55"))
        assert asyncio.run(apps[0]._claim_docs_login_shared("192.0.2.55")) == 0
    finally:
        database.close()


def test_refresh_token_is_one_time_use_across_app_instances(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'refresh_store.sqlite3'}")
    SECURITY_STORE_TABLE.create(database.engine)
    apps = [
        Application(database=database, security_storage="database")
        for _ in range(2)
    ]
    auths = [JWTAuth("r" * 32, security_store=app.security_store) for app in apps]
    for app, auth in zip(apps, auths):
        app.include_router(auth.router(lambda _username, _password: None, throttle={}))
    refresh_token = auths[0].create_refresh_token("user-1")

    async def run():
        first = await _http_request(
            apps[0], "/auth/refresh", method="POST", payload={"refresh_token": refresh_token}
        )
        replay = await _http_request(
            apps[1], "/auth/refresh", method="POST", payload={"refresh_token": refresh_token}
        )
        return first, replay

    try:
        first, replay = asyncio.run(run())
        assert first[0] == 200
        assert first[1]["refresh_token"] != refresh_token
        assert replay[0] == 401
    finally:
        database.close()


def test_sync_dependency_does_not_block_event_loop():
    app = Application(security_storage="memory")
    entered = threading.Event()
    release = threading.Event()

    def slow_dependency():
        entered.set()
        release.wait(timeout=3)
        return "ready"

    @app.get("/sync-dependency")
    async def endpoint(value=Depends(slow_dependency)):
        return {"value": value}

    async def run():
        request = asyncio.create_task(_http_request(app, "/sync-dependency"))
        assert await asyncio.to_thread(entered.wait, 1)
        assert not request.done(), "request should still be waiting for its dependency"
        release.set()
        return await request

    try:
        status, body = asyncio.run(run())
        assert status == 200
        assert body == {"value": "ready"}
    finally:
        release.set()


def test_sync_route_does_not_block_event_loop():
    app = Application(security_storage="memory")
    entered = threading.Event()
    release = threading.Event()

    @app.get("/sync-route")
    def endpoint():
        entered.set()
        release.wait(timeout=3)
        return {"value": "ready"}

    async def run():
        request = asyncio.create_task(_http_request(app, "/sync-route"))
        assert await asyncio.to_thread(entered.wait, 1)
        assert not request.done(), "request should still be waiting for its route"
        release.set()
        return await request

    try:
        status, body = asyncio.run(run())
        assert status == 200
        assert body == {"value": "ready"}
    finally:
        release.set()


@pytest.mark.parametrize(
    ("auth_kind", "headers"),
    [
        ("api_key", [(b"x-api-key", b"presented-key")]),
        ("basic", [(b"authorization", b"Basic YWRhOnBhc3M=")]),
        ("session", [(b"cookie", b"lenapi_session=session-id")]),
        ("opaque", [(b"authorization", b"Bearer opaque-token")]),
    ],
)
def test_sync_builtin_auth_lookups_do_not_block_event_loop(auth_kind, headers):
    app = Application(security_storage="memory")
    entered = threading.Event()
    release = threading.Event()

    def slow_lookup(*_args):
        entered.set()
        release.wait(timeout=3)
        return {"sub": "authenticated-user"}

    auth = {
        "api_key": lambda: APIKeyAuth(authenticate=slow_lookup),
        "basic": lambda: BasicAuth(slow_lookup),
        "session": lambda: SessionAuth(slow_lookup),
        "opaque": lambda: OpaqueBearerAuth(slow_lookup),
    }[auth_kind]()

    @app.get("/protected", auth=auth)
    async def protected():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    async def run():
        protected_request = asyncio.create_task(
            _http_request(app, "/protected", headers=headers)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        assert not protected_request.done()
        health_status, health_body = await asyncio.wait_for(
            _http_request(app, "/health"), timeout=0.5
        )
        release.set()
        protected_status, protected_body = await protected_request
        return health_status, health_body, protected_status, protected_body

    try:
        assert asyncio.run(run()) == (
            200, {"status": "ok"}, 200, {"ok": True}
        )
    finally:
        release.set()


def test_docs_auth_middleware_offloads_sync_authenticator():
    app = Application(security_storage="memory")
    entered = threading.Event()
    release = threading.Event()

    def slow_authenticator(_request):
        entered.set()
        release.wait(timeout=3)
        return {"sub": "docs-user"}

    @app.get("/private-docs")
    async def private_docs():
        return {"ok": True}

    app.add_middleware(
        DocsAuthMiddleware, auth=slow_authenticator, paths=("/private-docs",)
    )

    async def run():
        protected_request = asyncio.create_task(_http_request(app, "/private-docs"))
        assert await asyncio.to_thread(entered.wait, 1)
        assert not protected_request.done()
        health_status, health_body = await asyncio.wait_for(
            _http_request(app, "/health"), timeout=0.5
        )
        release.set()
        protected_status, protected_body = await protected_request
        return health_status, health_body, protected_status, protected_body

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    try:
        assert asyncio.run(run()) == (
            200, {"status": "ok"}, 200, {"ok": True}
        )
    finally:
        release.set()


def test_sync_hmac_nonce_store_does_not_block_event_loop():
    app = Application(security_storage="memory")
    entered = threading.Event()
    release = threading.Event()
    secret = "h" * 32

    class SlowNonceStore:
        def claim(self, _key, _ttl):
            entered.set()
            release.wait(timeout=3)
            return 0

    auth = HMACAuth(secret, nonce_store=SlowNonceStore())

    @app.post("/signed", auth=auth)
    async def signed():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    body = b"{}"
    timestamp = str(int(time.time()))
    nonce = "request-nonce"
    signature = hmac_request_signature(
        secret, "POST", "/signed", timestamp, body, nonce=nonce
    )

    async def run():
        protected_request = asyncio.create_task(_http_request(
            app,
            "/signed",
            method="POST",
            payload={},
            headers=[
                (b"x-lenapi-timestamp", timestamp.encode()),
                (b"x-lenapi-nonce", nonce.encode()),
                (b"x-lenapi-signature", signature.encode()),
            ],
        ))
        assert await asyncio.to_thread(entered.wait, 1)
        assert not protected_request.done()
        health_status, health_body = await asyncio.wait_for(
            _http_request(app, "/health"), timeout=0.5
        )
        release.set()
        protected_status, protected_body = await protected_request
        return health_status, health_body, protected_status, protected_body

    try:
        assert asyncio.run(run()) == (
            200, {"status": "ok"}, 200, {"ok": True}
        )
    finally:
        release.set()


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
