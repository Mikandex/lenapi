"""End-to-end feature coverage for LenApi's public framework APIs.

Network-backed services use HTTPX mock transports, and MongoDB is tested with
the in-memory fake driver in test_mongodb.py. The suite does not need external
databases, Redis, or provider credentials.
"""

import asyncio
import base64
from datetime import date, datetime, time as time_type, timedelta
import hashlib
import hmac
import importlib
import json
import threading
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import CheckConstraint, insert, inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError

import lenapi
from lenapi import (
    APIKeyAuth,
    Application,
    AsyncDatabase,
    BasicAuth,
    Body,
    CSRFProtect,
    Database,
    Depends,
    HTTPException,
    HMACAuth,
    JWTAuth,
    LENAPI,
    Model,
    OIDCAuth,
    OpaqueBearerAuth,
    Request,
    Response,
    Router,
    SessionAuth,
    WebhookAuth,
    create_authenticator,
    hash_api_key,
    hmac_request_signature,
    make_api_key,
    make_password,
    check_password,
)
from lenapi.authorization import require_owner, require_permission, require_role
from lenapi.model import (
    BooleanField,
    CASCADE,
    Count,
    DecimalField,
    DateField,
    DateTimeField,
    DurationField,
    EmailField,
    FileField,
    FileImageField,
    ForeignKeyField,
    IPField,
    JSONField,
    ManyToManyField,
    Max,
    Min,
    PROTECT,
    OneToOneField,
    PhoneField,
    PositiveBigIntegerField,
    PositiveIntegerField,
    SET_NULL,
    StringField,
    Sum,
    TimeField,
    URLField,
    UUIDField,
)
from lenapi.models import reset_current_session, set_current_session
from lenapi.security_store import DatabaseSecurityStore, create_security_store


def asgi_request(app, path="/", *, method="GET", body=b"", headers=(), query=b"", client_ip="127.0.0.1"):
    async def run():
        messages = []
        sent_request = False

        async def receive():
            nonlocal sent_request
            if sent_request:
                return {"type": "http.disconnect"}
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query, "headers": list(headers), "client": (client_ip, 12345),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_body = next(message for message in messages if message["type"] == "http.response.body")["body"]
        return start["status"], start["headers"], response_body

    return asyncio.run(run())


def json_request(app, path, *, method="GET", payload=None, headers=(), query=b""):
    body = b"" if payload is None else json.dumps(payload).encode()
    common_headers = list(headers)
    if payload is not None:
        common_headers.append((b"content-type", b"application/json"))
    status, headers_out, body_out = asgi_request(
        app, path, method=method, body=body, headers=common_headers, query=query
    )
    return status, dict(headers_out), json.loads(body_out or b"null")


def make_app(**kwargs):
    return Application(database=Database("sqlite://"), security_storage="memory", **kwargs)


def test_lenapi_is_the_only_public_application_class():
    assert lenapi.LENAPI is LENAPI
    assert not hasattr(lenapi, "LenApi")
    app = LENAPI(database=Database("sqlite://"), security_storage="memory")
    assert isinstance(app, Application)
    assert not {route.path for route in app.routes}.intersection(
        {"/auth/register", "/auth/token", "/auth/refresh", "/auth/profile"}
    )
    app.database.close()


def test_multiple_routers_validation_response_models_and_openapi_visibility():
    class Input(BaseModel):
        name: str

    class Output(BaseModel):
        model_config = ConfigDict(from_attributes=True)
        message: str

    app = make_app()
    api = Router(prefix="/api/")
    internal = Router(prefix="/internal")

    @api.post("/hello", response_model=Output)
    async def hello(data: Input):
        return {"message": f"Hello, {data.name}!", "extra": "filtered"}

    @internal.get("/status", include_in_schema=False)
    def status():
        return {"ok": True}

    app.include_router(api)
    app.include_router(internal)

    status_code, _headers, result = json_request(app, "/api/hello", method="POST", payload={"name": "Ada"})
    assert status_code == 200
    assert result == {"message": "Hello, Ada!"}
    assert json_request(app, "/api/hello", method="POST", payload={"wrong": "shape"})[0] == 422
    schema = json_request(app, "/openapi.json")[2]
    assert "/api/hello" in schema["paths"]
    assert "/internal/status" not in schema["paths"]
    app.database.close()


def test_typed_path_query_body_request_and_background_tasks():
    app = make_app()
    completed = []

    @app.post("/items/{item_id}")
    async def save_item(item_id: int, amount: int, data: dict = Body(), request: Request = None):
        request.background_tasks.add_task(completed.append, (item_id, amount, data["value"]))
        return {"query": amount, "ip": request.client_ip}

    status, _headers, response = json_request(
        app, "/items/12", method="POST", payload={"value": "v"}, query=b"amount=5"
    )
    assert status == 200
    assert response == {"query": 5, "ip": "127.0.0.1"}
    assert completed == [(12, 5, "v")]
    assert json_request(app, "/items/not-an-int", method="POST", payload={"value": 1}, query=b"amount=1")[0] == 422
    app.database.close()


def test_dependency_results_are_cached_and_cycles_return_422():
    app = make_app()
    calls = []

    def identity():
        calls.append("called")
        return {"user": "ada"}

    @app.get("/cached")
    def cached(first=Depends(identity), second=Depends(identity)):
        return {"same": first is second}

    def invalid_dependency(required_value: int):
        return required_value

    @app.get("/invalid-dependency")
    def missing_dependency(value=Depends(invalid_dependency)):
        return value

    assert json_request(app, "/cached")[2] == {"same": True}
    assert calls == ["called"]
    assert json_request(app, "/invalid-dependency")[0] == 422
    assert json_request(app, "/missing")[0] == 404
    app.database.close()


def test_http_exceptions_custom_responses_and_cookie_headers():
    app = make_app()

    @app.get("/denied")
    def denied():
        raise HTTPException(403, "no", headers={"X-Reason": "policy"})

    @app.get("/cookie")
    def cookie():
        response = Response("ok")
        response.set_cookie("session", "abc", secure=True, httponly=True, max_age=60)
        return response

    status, headers, body = json_request(app, "/denied")
    assert status == 403 and body == {"detail": "no"}
    assert dict(headers)[b"x-reason"] == b"policy"
    status, headers, body = asgi_request(app, "/cookie")
    assert status == 200 and body == b"ok"
    cookie_headers = [value.decode() for key, value in headers if key == b"set-cookie"]
    assert any("HttpOnly" in value and "Secure" in value for value in cookie_headers)
    app.database.close()


def test_request_limits_form_parsing_and_request_body_cache():
    app = make_app(max_request_body_size=4)

    @app.post("/body")
    async def body(data: dict = Body()):
        return data

    status, _headers, payload = json_request(app, "/body", method="POST", payload={"x": 1})
    assert status == 413 and payload["detail"] == "Request body too large"

    from lenapi.request import Request
    messages = iter([{"type": "http.request", "body": b"a=1&a=2", "more_body": False}])

    async def receive():
        return next(messages)

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [
        (b"content-type", b"application/x-www-form-urlencoded")
    ]}, receive)
    assert asyncio.run(request.form()) == {"a": ["1", "2"]}
    assert asyncio.run(request.body()) == b"a=1&a=2"
    app.database.close()


def test_jwt_api_key_and_basic_auth_routes_and_openapi_security():
    app = make_app()
    jwt = JWTAuth("j" * 32, issuer="lenapi-tests")
    api_key = APIKeyAuth("k" * 32)
    basic = BasicAuth(lambda user, password: {"sub": user} if password == "pass" else None)

    @app.get("/jwt")
    def jwt_route(identity=Depends(jwt)):
        return identity

    @app.get("/key")
    def key_route(identity=Depends(api_key)):
        return identity

    @app.get("/basic")
    def basic_route(identity=Depends(basic)):
        return identity

    token = jwt.create_access_token("7", claims={"role": "admin"})
    assert json_request(app, "/jwt", headers=[(b"authorization", f"Bearer {token}".encode())])[2]["role"] == "admin"
    assert json_request(app, "/jwt")[0] == 401
    assert json_request(app, "/jwt", headers=[(b"authorization", f"Bearer {jwt.create_refresh_token('7')}".encode())])[0] == 401
    assert json_request(app, "/key", headers=[(b"x-api-key", ("k" * 32).encode())])[0] == 200
    basic_value = base64.b64encode(b"ada:pass")
    assert json_request(app, "/basic", headers=[(b"authorization", b"Basic " + basic_value)])[2] == {"sub": "ada"}
    schema = json_request(app, "/openapi.json")[2]
    assert {"BearerAuth", "ApiKey_x_api_key", "BasicAuth"}.issubset(schema["components"]["securitySchemes"])
    app.database.close()


def test_jwt_token_and_refresh_routes_are_throttled_by_default_and_configurable():
    app = make_app()
    jwt = JWTAuth("t" * 32)
    app.include_router(jwt.router(lambda _username, _password: None, throttle={"ip": "1/minute"}))

    first = json_request(
        app, "/auth/token", method="POST",
        payload={"username": "ada", "password": "wrong"},
    )
    second = json_request(
        app, "/auth/token", method="POST",
        payload={"username": "ada", "password": "wrong"},
    )
    refresh_first = json_request(
        app, "/auth/refresh", method="POST",
        payload={"refresh_token": jwt.create_refresh_token("1")},
    )
    refresh_second = json_request(
        app, "/auth/refresh", method="POST",
        payload={"refresh_token": jwt.create_refresh_token("1")},
    )

    assert first[0] == 401
    assert second[0] == 429
    assert refresh_first[0] == 200
    assert refresh_second[0] == 429
    app.database.close()


def test_anonymous_throttle_rejects_before_route_authentication():
    app = make_app()
    calls = []

    def authenticate(username, password):
        calls.append((username, password))
        return None

    @app.get("/protected", auth=BasicAuth(authenticate), throttle={"anonymous": "1/minute"})
    def protected():
        return {"ok": True}

    credentials = base64.b64encode(b"ada:wrong")
    headers = [(b"authorization", b"Basic " + credentials)]
    first = json_request(app, "/protected", headers=headers)
    second = json_request(app, "/protected", headers=headers)

    assert first[0] == 401
    assert second[0] == 429
    assert calls == [("ada", "wrong")]
    app.database.close()


def test_basic_auth_gets_a_default_pre_auth_limit():
    app = make_app()
    calls = []

    def authenticate(username, password):
        calls.append((username, password))
        return None

    @app.get("/basic-limited", auth=BasicAuth(authenticate))
    def basic_limited():
        return {"ok": True}

    credentials = base64.b64encode(b"ada:wrong")
    headers = [(b"authorization", b"Basic " + credentials)]
    results = [json_request(app, "/basic-limited", headers=headers) for _ in range(11)]

    assert all(result[0] == 401 for result in results[:10])
    assert results[10][0] == 429
    assert len(calls) == 10
    app.database.close()


def test_database_auth_runs_dummy_password_check_for_unknown_user(monkeypatch):
    from importlib import import_module

    authentication_module = import_module("lenapi.authentication")

    class MissingUserModel:
        __table__ = SimpleNamespace(columns={"username": object(), "password": object()})

        @classmethod
        def get(cls, **_filters):
            return SimpleNamespace(get=lambda: None)

    checks = []

    def check_password(password, encoded_hash):
        checks.append((password, encoded_hash))
        return False

    monkeypatch.setattr(authentication_module, "check_password", check_password)
    authenticate = authentication_module._user_authenticator(MissingUserModel)

    assert authenticate("missing-user", "incorrect-password") is None
    assert checks == [("incorrect-password", authentication_module._DUMMY_PASSWORD_HASH)]


def test_jwt_auth_router_has_default_ip_throttle():
    jwt = JWTAuth("u" * 32)
    router = jwt.router(lambda _username, _password: None)
    for route in router.routes:
        assert route.throttle is not None
        assert len(route.throttle) == 1
        assert (route.throttle[0].scope, route.throttle[0].count, route.throttle[0].window_seconds) == (
            "ip", 10, 60
        )


def test_router_and_route_level_authentication_and_openapi():
    app = make_app()
    jwt = JWTAuth("r" * 32)
    basic = BasicAuth(lambda username, password: {"sub": username} if password == "correct" else None)
    api = Router(prefix="/api", auth=jwt)

    class Greeting(BaseModel):
        name: str

    @api.post("/hello")
    async def hello(data: Greeting):
        return {"message": f"Hello, {data.name}!"}

    @api.get("/identity")
    def identity(claims=Depends(jwt)):
        return claims

    @api.get("/basic", auth=basic)
    def basic_route():
        return {"authenticated": True}

    app.include_router(api)
    assert json_request(app, "/api/hello", method="POST", payload={"name": "Ada"})[0] == 401
    token = jwt.create_access_token("42", claims={"role": "member"})
    headers = [(b"authorization", f"Bearer {token}".encode())]
    status, _, payload = json_request(app, "/api/hello", method="POST", payload={"name": "Ada"}, headers=headers)
    assert status == 200 and payload == {"message": "Hello, Ada!"}
    assert json_request(app, "/api/identity", headers=headers)[2]["role"] == "member"
    assert json_request(app, "/api/basic", headers=headers)[0] == 401
    basic_credentials = base64.b64encode(b"ada:correct")
    assert json_request(app, "/api/basic", headers=[(b"authorization", b"Basic " + basic_credentials)])[0] == 200
    schema = json_request(app, "/openapi.json")[2]
    assert schema["paths"]["/api/hello"]["post"]["security"] == [{"BearerAuth": []}]
    assert schema["paths"]["/api/basic"]["get"]["security"] == [{"BasicAuth": []}]
    app.database.close()


def test_jwt_tokens_expire_reject_reserved_claims_and_validate_issuer():
    jwt = JWTAuth("s" * 32, issuer="expected")
    with pytest.raises(ValueError, match="reserved"):
        jwt.create_access_token("user", claims={"sub": "attacker"})
    assert jwt.decode_token(jwt.create_access_token("user"))["iss"] == "expected"
    with pytest.raises(ValueError):
        jwt.decode_token(JWTAuth("x" * 32, issuer="other").create_access_token("user"))
    with pytest.raises(ValueError):
        jwt.decode_token(jwt.create_access_token("user", expires_in=1)[:-2] + "xx")
    with pytest.raises(ValueError, match="positive"):
        JWTAuth("s" * 32, token_lifetime_seconds=0)


def test_session_opaque_and_oidc_authentication_support_sync_and_async_lookups():
    app = make_app()
    async def lookup_session(value):
        return {"sub": "session-user"} if value == "sid" else None
    async def lookup_token(value):
        return {"sub": "token-user"} if value == "opaque" else None
    session = SessionAuth(lookup_session)
    opaque = OpaqueBearerAuth(lookup_token)
    oidc = OIDCAuth(lookup_token)

    @app.get("/session")
    def session_route(identity=Depends(session)):
        return identity

    @app.get("/opaque")
    def opaque_route(identity=Depends(opaque)):
        return identity

    @app.get("/oidc")
    def oidc_route(identity=Depends(oidc)):
        return identity

    assert json_request(app, "/session", headers=[(b"cookie", b"lenapi_session=sid")])[2]["sub"] == "session-user"
    assert json_request(app, "/opaque", headers=[(b"authorization", b"Bearer opaque")])[2]["sub"] == "token-user"
    assert json_request(app, "/oidc", headers=[(b"authorization", b"Bearer opaque")])[2]["sub"] == "token-user"
    assert json_request(app, "/session")[0] == 401
    app.database.close()


def test_hmac_and_webhook_signatures_reject_replay_and_deduplicate_events():
    secret = "h" * 32
    body = b'{"event":"created"}'
    now = str(int(time.time()))
    hmac_auth = HMACAuth(secret)
    signature = hmac_request_signature(secret, "POST", "/signed", now, body, nonce="n-1")
    app = make_app()

    @app.post("/signed")
    async def signed(identity=Depends(hmac_auth), data: dict = Body()):
        return {"auth": identity["auth_method"], "data": data}

    signed_headers = [
        (b"x-lenapi-timestamp", now.encode()), (b"x-lenapi-nonce", b"n-1"),
        (b"x-lenapi-signature", signature.encode()), (b"content-type", b"application/json"),
    ]
    assert asgi_request(app, "/signed", method="POST", body=body, headers=signed_headers)[0] == 200
    assert asgi_request(app, "/signed", method="POST", body=body, headers=signed_headers)[0] == 401

    class ClaimStore:
        seen = set()
        async def claim(self, key, ttl):
            duplicate = key in self.seen
            self.seen.add(key)
            return int(duplicate)

    store = ClaimStore()
    webhook = WebhookAuth(secret, security_store=store, provider="test")
    webhook_signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    hook_app = make_app()

    @hook_app.post("/hook")
    async def hook(identity=Depends(webhook)):
        return identity

    headers = [(b"x-webhook-signature", webhook_signature.encode()), (b"x-webhook-id", b"event-1")]
    assert asgi_request(hook_app, "/hook", method="POST", body=body, headers=headers)[0] == 200
    assert asgi_request(hook_app, "/hook", method="POST", body=body, headers=headers)[0] == 200
    assert json.loads(asgi_request(hook_app, "/hook", method="POST", body=body, headers=headers)[2])[
        "detail"
    ] == "Webhook event already received"
    app.database.close()
    hook_app.database.close()


def test_csrf_guard_safe_method_cookie_header_and_bad_signature():
    csrf = CSRFProtect("c" * 32, secure=False)
    app = make_app()

    @app.get("/read")
    def read(_csrf=Depends(csrf)):
        return {"ok": True}

    @app.post("/write")
    def write(_csrf=Depends(csrf)):
        return {"ok": True}

    assert json_request(app, "/read")[0] == 200
    token = csrf.generate_token()
    headers = [(b"cookie", f"{csrf.cookie_name}={token}".encode()), (b"x-csrf-token", token.encode())]
    assert json_request(app, "/write", method="POST", headers=headers)[0] == 200
    assert json_request(app, "/write", method="POST")[0] == 403
    app.database.close()


def test_password_api_key_and_authenticator_factory_helpers():
    encoded = make_password("secret-pass")
    assert check_password("secret-pass", encoded)
    assert not check_password("wrong", encoded)
    key = make_api_key()
    assert len(key) >= 40 and key.isascii()
    assert hash_api_key(key) != key
    settings = SimpleNamespace(AUTHENTICATION="jwt", TOKEN={"SECRET_KEY": "x" * 32})
    assert isinstance(create_authenticator(settings), JWTAuth)
    assert isinstance(create_authenticator(SimpleNamespace(AUTHENTICATION="api_key", API_KEY="k" * 32)), APIKeyAuth)
    with pytest.raises(ValueError, match="Unknown"):
        create_authenticator(SimpleNamespace(AUTHENTICATION="invalid"))


def test_authorization_role_permission_and_owner_dependencies():
    app = make_app()
    auth = lambda: {"sub": "12", "roles": ["editor"], "permissions": ["read", "write"]}
    role = require_role("admin", "editor", auth=auth)
    permission = require_permission("read", "write", match_all=True, auth=auth)
    main_thread_id = threading.get_ident()
    loader_thread_ids = []

    def load_item(item_id):
        loader_thread_ids.append(threading.get_ident())
        return {"user_id": 12, "id": item_id}

    owner = require_owner(
        load_item, auth=auth, path_param="item_id"
    )

    @app.get("/role")
    def role_route(identity=Depends(role)):
        return identity

    @app.get("/permission")
    def permission_route(identity=Depends(permission)):
        return {"sub": identity["sub"]}

    @app.get("/items/{item_id}")
    def item_route(item=Depends(owner)):
        return item

    assert json_request(app, "/role")[0] == 200
    assert json_request(app, "/permission")[2] == {"sub": "12"}
    status, _headers, result = json_request(app, "/items/7")
    assert status == 200 and result["user_id"] == 12
    assert loader_thread_ids and loader_thread_ids[0] != main_thread_id
    app.database.close()


class FeatureRecord(Model):
    name = StringField(max_length=80, unique=True)
    group = StringField(max_length=40)
    amount = DecimalField(default=Decimal("0.00"))
    active = BooleanField(default=True)
    info = JSONField(default=dict)
    public_id = UUIDField(default=uuid.uuid4, unique=True)


class ParentRecord(Model):
    name = StringField(max_length=40)


class CascadeChild(Model):
    parent_id = ForeignKeyField("parentrecord.id", model=CASCADE)


class NullChild(Model):
    parent_id = ForeignKeyField("parentrecord.id", model=SET_NULL, nullable=True)


class ProtectedChild(Model):
    parent_id = ForeignKeyField("parentrecord.id", model=PROTECT)


class TagRecord(Model):
    name = StringField(max_length=40)


class ArticleRecord(Model):
    title = StringField(max_length=80)
    tags = ManyToManyField("TagRecord")


class ProfileRecord(Model):
    parent_id = OneToOneField("parentrecord.id", nullable=False)


class PositiveFieldRecord(Model):
    count = PositiveIntegerField(nullable=False)
    total = PositiveBigIntegerField(default=0, nullable=False)


class ValidatedFieldRecord(Model):
    email = EmailField(error_messages={"email": "this is not an email"})
    phone = PhoneField()
    address = IPField()
    website = URLField()
    public_id = UUIDField()


class UniqueMessageRecord(Model):
    email = EmailField(unique=True, error_messages={"unique": "Already exists"})


class TimestampDurationRecord(Model):
    created_date = DateField()
    created_time = TimeField()
    created_at = DateTimeField()
    trial_period = DurationField(weeks=1)


class StringPrimaryKeyRecord(Model):
    code = StringField(max_length=12, primary_key=True)


class CustomKeyTagRecord(Model):
    code = StringField(max_length=12, primary_key=True)


class CustomKeyItemRecord(Model):
    sku = StringField(max_length=12, primary_key=True)
    tags = ManyToManyField("CustomKeyTagRecord")


def test_model_fields_defaults_uuid_unique_indexes_and_all_crud_helpers(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'model-features.db'}")
    FeatureRecord.__table__.create(db.engine)
    session = db.session()
    token = set_current_session(session)
    try:
        one = FeatureRecord.create(name="Ada", group="math", amount=Decimal("12.50"), info={"active": True})
        FeatureRecord.create(name="Grace", group="systems", amount=Decimal("25.00"))
        FeatureRecord.create(name="Alan", group="math", amount=Decimal("7.50"))
        assert one.id == 1 and isinstance(one.public_id, uuid.UUID) and one.active is True
        assert FeatureRecord.get(name="Ada").get().info == {"active": True}
        assert FeatureRecord.get_or_404(name="Grace").amount == Decimal("25.00")
        assert FeatureRecord.get(name="missing").exists() is False
        assert FeatureRecord.get_or_create(name="Ada")[1] is False
        assert FeatureRecord.get_or_create({"group": "logic"}, name="Turing")[0].group == "logic"
        assert FeatureRecord.create_update({"amount": Decimal("13")}, name="Ada").amount == Decimal("13")
        assert FeatureRecord.update_or_404({"group": "theory"}, name="Ada").group == "theory"
        assert FeatureRecord.filter(amount__gte=10).order("-amount").limit(1).all()[0].name == "Grace"
        assert FeatureRecord.filter(group="math").values("name").all() == ["Alan"]
        assert FeatureRecord.search(name="ada").count() == 1
        assert FeatureRecord.aggregate(total=Sum("amount"), count=Count(), low=Min("amount"), high=Max("amount"))["count"] == 4
        grouped = FeatureRecord.aggregate(group_by="group", total=Sum("amount"))
        assert {row["group"] for row in grouped} == {"math", "systems", "logic", "theory"}
        updated = FeatureRecord.get(name="Ada").get()
        updated.group = "updated"
        updated.save()
        assert FeatureRecord.delete_or_404(name="Turing") is True
        assert FeatureRecord.filter(group="math").delete() == 1
        assert FeatureRecord.all().count() == 2
        assert sa_inspect(FeatureRecord).columns.name.unique is True
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def test_foreign_key_delete_policies_and_many_to_many_relationships(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'relations.db'}")
    tables = [ParentRecord.__table__, CascadeChild.__table__, NullChild.__table__, ProtectedChild.__table__]
    Model.metadata.create_all(db.engine, tables=tables)
    session = db.session()
    token = set_current_session(session)
    try:
        parent = ParentRecord.create(name="cascade")
        CascadeChild.create(parent_id=parent.id)
        assert parent.delete() is True
        assert session.scalar(select(CascadeChild)) is None

        parent = ParentRecord.create(name="nullable")
        NullChild.create(parent_id=parent.id)
        parent.delete()
        assert session.scalar(select(NullChild)).parent_id is None

        parent = ParentRecord.create(name="protected")
        ProtectedChild.create(parent_id=parent.id)
        with pytest.raises(HTTPException, match="protected records"):
            parent.delete()
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def test_new_fields_validate_values_and_default_temporal_fields(tmp_path):
    assert ProfileRecord.__table__.c.parent_id.unique is True
    assert "code" in StringPrimaryKeyRecord.__table__.primary_key.columns
    assert "id" not in StringPrimaryKeyRecord.__table__.columns
    check_names = {
        constraint.name for constraint in PositiveFieldRecord.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert check_names == {
        "ck_positivefieldrecord_count_non_negative",
        "ck_positivefieldrecord_total_non_negative",
    }

    with pytest.raises(HTTPException) as invalid_email:
        ValidatedFieldRecord(
            email="not-an-email", phone="+123456789", address="127.0.0.1",
            website="https://example.com", public_id=uuid.uuid4(),
        ).save()
    assert invalid_email.value.detail == "this is not an email"

    for field, value in (
        ("phone", "123456"),
        ("address", "999.1.1.1"),
        ("website", "ftp://example.com"),
        ("public_id", "not-a-uuid"),
    ):
        values = {
            "email": "ada@example.com", "phone": "+123456789", "address": "127.0.0.1",
            "website": "https://example.com", "public_id": uuid.uuid4(),
        }
        values[field] = value
        with pytest.raises(HTTPException):
            ValidatedFieldRecord(**values).save()

    db = Database(f"sqlite:///{tmp_path / 'new-model-fields.db'}")
    tables = [
        PositiveFieldRecord.__table__, TimestampDurationRecord.__table__,
        UniqueMessageRecord.__table__,
    ]
    Model.metadata.create_all(db.engine, tables=tables)
    session = db.session()
    token = set_current_session(session)
    try:
        with pytest.raises(HTTPException, match="zero or greater"):
            PositiveFieldRecord.create(count=-1)
        with pytest.raises(IntegrityError):
            with db.engine.begin() as connection:
                connection.execute(insert(PositiveFieldRecord.__table__).values(count=-1, total=0))

        record = TimestampDurationRecord.create()
        assert isinstance(record.created_date, date)
        assert isinstance(record.created_time, time_type)
        assert isinstance(record.created_at, datetime)
        assert record.trial_period == timedelta(days=7)

        UniqueMessageRecord.create(email="ada@example.com")
        with pytest.raises(HTTPException) as duplicate:
            UniqueMessageRecord.create(email="ada@example.com")
        assert duplicate.value.detail == "Already exists"
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def test_many_to_many_supports_custom_primary_keys(tmp_path):
    association = CustomKeyItemRecord.tags.property.secondary
    assert {str(foreign_key.target_fullname) for column in association.columns for foreign_key in column.foreign_keys} == {
        "customkeyitemrecord.sku", "customkeytagrecord.code",
    }
    db = Database(f"sqlite:///{tmp_path / 'custom-primary-keys.db'}")
    Model.metadata.create_all(
        db.engine,
        tables=[CustomKeyItemRecord.__table__, CustomKeyTagRecord.__table__, association],
    )
    session = db.session()
    token = set_current_session(session)
    try:
        item = CustomKeyItemRecord.create(sku="item-1")
        tag = CustomKeyTagRecord.create(code="tag-1")
        item.tags.add(tag)
        session.commit()
        assert item.tags.all() == [tag]
    finally:
        reset_current_session(token)
        session.close()
        db.close()

    db = Database(f"sqlite:///{tmp_path / 'many-to-many.db'}")
    Model.metadata.create_all(db.engine, tables=[ArticleRecord.__table__, TagRecord.__table__, ArticleRecord.tags.property.secondary])
    session = db.session()
    token = set_current_session(session)
    try:
        article = ArticleRecord.create(title="Frameworks")
        tag = TagRecord.create(name="Python")
        article.tags.add(tag)
        session.commit()
        assert [item.name for item in ArticleRecord.get(id=article.id).get().tags.all()] == ["Python"]
        article.tags.set([])
        session.commit()
        assert not article.tags
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def test_model_invalid_filters_pagination_and_duplicate_unique_values(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'model-invalid.db'}")
    FeatureRecord.__table__.create(db.engine)
    session = db.session()
    token = set_current_session(session)
    try:
        FeatureRecord.create(name="Unique", group="a")
        with pytest.raises(HTTPException, match="Double entries"):
            FeatureRecord.create(name="Unique", group="b")
        with pytest.raises(HTTPException, match="limit"):
            FeatureRecord.filter().limit(-1)
        with pytest.raises(HTTPException, match="offset"):
            FeatureRecord.filter().offset(-1)
        with pytest.raises(ValueError, match="Unknown field"):
            FeatureRecord.filter(does_not_exist=1).all()
    finally:
        reset_current_session(token)
        session.close()
        db.close()


def test_database_security_store_hashes_keys_expires_and_claims_once(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'security-store.db'}")
    from lenapi.models import SECURITY_STORE_TABLE
    SECURITY_STORE_TABLE.create(db.engine)
    store = DatabaseSecurityStore(db.engine)

    async def run():
        await store.set("session-secret", "value", ttl=30)
        assert await store.get("session-secret") == "value"
        assert await store.claim("event:1", 30) == 0
        assert await store.claim("event:1", 30) > 0
        await store.delete("session-secret")
        assert await store.get("session-secret") is None
        from lenapi.security_store import SecurityStoreUnavailable
        with pytest.raises(SecurityStoreUnavailable):
            await store.set("bad", "value", ttl=0)

    try:
        asyncio.run(run())
        with db.engine.connect() as connection:
            stored_keys = connection.execute(select(SECURITY_STORE_TABLE.c.key)).scalars().all()
        assert all(len(key) == 64 and key != "session-secret" for key in stored_keys)
        assert create_security_store("memory") is None
        with pytest.raises(ValueError, match="application Database"):
            create_security_store("database")
    finally:
        db.close()


def test_ai_services_use_mocked_http_and_cover_openai_and_ollama(monkeypatch):
    class StubResponse:
        def __init__(self, url):
            self.url = url
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True, "path": urlsplit(self.url).path}

    class StubAsyncClient:
        def __init__(self):
            self.paths = []
        async def post(self, url, **kwargs):
            self.paths.append(url)
            return StubResponse(url)
        async def aclose(self):
            pass

    client = StubAsyncClient()

    async def run():
        from lenapi.service import OpenAI, Ollama
        async with OpenAI("secret", base_url="https://mock/v1", client=client) as openai:
            assert (await openai.chat([{"role": "user", "content": "hi"}]))["ok"]
            assert (await openai.responses("hi"))["path"] == "/v1/responses"
            assert (await openai.embeddings("hi"))["path"] == "/v1/embeddings"
        async with Ollama(base_url="http://mock", client=client) as ollama:
            assert (await ollama.chat([{"role": "user", "content": "hi"}]))["path"] == "/api/chat"
            assert (await ollama.generate("hello"))["path"] == "/api/generate"
            assert (await ollama.embeddings("hello"))["path"] == "/api/embed"
        await client.aclose()

    asyncio.run(run())
    assert len(client.paths) == 6
    assert client.paths == [
        "https://mock/v1/chat/completions", "https://mock/v1/responses",
        "https://mock/v1/embeddings", "http://mock/api/chat",
        "http://mock/api/generate", "http://mock/api/embed",
    ]


def test_authentication_factory_modes_and_database_user_password_helpers():
    class TestAuthUser(Model):
        username = StringField(max_length=64, unique=True)
        password = StringField(max_length=255)

    settings = SimpleNamespace(AUTHENTICATION="basic_login", BASIC_AUTH_FIELD="username")
    from lenapi.authentication import basic, authentication_type, configure, register_user
    assert isinstance(basic(TestAuthUser), BasicAuth)
    assert isinstance(configure(settings, TestAuthUser), BasicAuth)
    jwt_settings = SimpleNamespace(AUTHENTICATION="jwt", TOKEN={"SECRET_KEY": "z" * 32})
    assert isinstance(authentication_type("jwt", settings=jwt_settings), JWTAuth)
    from lenapi import authentication
    assert isinstance(authentication(type="jwt", settings=jwt_settings), JWTAuth)
    assert isinstance(authentication_type("oidc", settings=SimpleNamespace(
        AUTHENTICATION="oidc", OIDC_TOKEN_LOOKUP=lambda token: {"sub": token}
    )), OIDCAuth)
    assert callable(register_user)


def test_custom_model_api_key_field_is_required_validated_and_used(monkeypatch):
    class CustomerForAPIKey(Model):
        username = StringField(max_length=64, unique=True)
        password = StringField(max_length=255)
        first_name = StringField(max_length=80, default="")
        last_name = StringField(max_length=80, default="")
        key_digest = StringField(max_length=64, unique=True, nullable=True)

    from lenapi import authentication
    from lenapi.authentication import register_user

    settings = SimpleNamespace(AUTHENTICATION="jwt")
    assert isinstance(authentication("api_key", settings=settings), APIKeyAuth)
    with pytest.raises(ValueError, match="requires api_key_field"):
        authentication("api_key", settings=settings, user_model=CustomerForAPIKey)
    with pytest.raises(ValueError, match="not a field"):
        authentication(
            "api_key", settings=settings, user_model=CustomerForAPIKey,
            api_key_field="missing_digest",
        )

    filters = []

    class QueryResult:
        def get(self):
            return SimpleNamespace(id=42)

    def fake_get(cls, **kwargs):
        filters.append(kwargs)
        return QueryResult()

    monkeypatch.setattr(CustomerForAPIKey, "get", classmethod(fake_get))
    auth = authentication(
        "api_key", settings=settings, user_model=CustomerForAPIKey,
        api_key_field="key_digest",
    )
    assert auth.authenticate("raw-client-key") == {
        "user_id": "42", "auth_method": "api_key"
    }
    assert filters == [{"key_digest": hash_api_key("raw-client-key")}]

    auth_module = importlib.import_module("lenapi.authentication")
    monkeypatch.setattr(auth_module, "make_api_key", lambda: "new-raw-key")
    created = {}

    def fake_create(cls, **kwargs):
        created.update(kwargs)
        return SimpleNamespace(
            id=1, username=kwargs["username"], first_name=kwargs["first_name"],
            last_name=kwargs["last_name"],
        )

    monkeypatch.setattr(CustomerForAPIKey, "create", classmethod(fake_create))
    registration = SimpleNamespace(
        username="ada", first_name="Ada", last_name="Lovelace", password="secret-pass"
    )
    result = register_user(
        user_model=CustomerForAPIKey, data=registration,
        authentication="api_key", api_key_field="key_digest",
    )
    assert result["api_key"] == "new-raw-key"
    assert created["key_digest"] == hash_api_key("new-raw-key")
