# lenApi

**LenAPI** is a modern ASGI framework shaped by real-world experience building with Python backend frameworks. It brings routing, Pydantic validation, synchronous and asynchronous database access, migrations, and interactive API documentation together in a developer-friendly toolkit.

LenAPI includes tools for common backend needs, including WebSockets, caching, background tasks, and authentication. Its social sign-in routes currently support Google, GitHub, and LinkedIn, with provider credentials and callback URLs configured by the developer.

LenAPI aims to reduce repetitive code and simplify common integrations, helping developers build APIs with less setup. Its framework source is compact, while its full-featured installation includes database and server dependencies.

**LenAPI — Compact by Design, Powerful by Capability.**

```python
from pydantic import BaseModel, ConfigDict
from lenapi import LENAPI, Router

app = LENAPI()
api = Router(prefix="/api")

class GreetingInput(BaseModel):
    name: str

class GreetingOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    message: str

@api.post("/hello", response_model=GreetingOutput)
async def hello(data: GreetingInput):
    return {"message": f"Hello, {data.name}!"}

app.include_router(api)
```

Set a Swagger tag for every route when mounting a router with `tag_name`; it overrides tags set on the router or individual decorators:

```python
app.include_router(social_auth.router(), prefix="/api", tag_name="Social Auth")
```

Use lenApi's standard-library logger in application code. `logger` is the default `lenapi` logger; `get_logger()` returns it or a namespaced child logger:

```python
from lenapi import get_logger, logger

logger.info("Application started")

auth_logger = get_logger("auth")
auth_logger.warning("Login attempt rejected")
```

LenApi prints `INFO` and higher by default. You can change the level or add your own handlers with Python's standard `logging` module.

## WebSockets

Register WebSocket handlers with `@app.websocket(...)` and use the connection helpers from `lenapi.service`:

```python
from lenapi import LENAPI
from lenapi.service import WebSocket, WebSocketDisconnect

app = LENAPI()

@app.websocket("/ws/{room_id}")
async def room_socket(websocket: WebSocket, room_id: int):
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_json({"room_id": room_id, "message": message})
    except WebSocketDisconnect:
        pass
```

`WebSocket` supports text, bytes, and JSON messages, explicit acceptance, and close codes. WebSocket paths can also be registered on a `Router` and mounted with `include_router()` or `add_router()`.

For a ready-made room relay, use `WebSocketService`. The primary types are `chat`, `video`, `json`, `message`, and `byte`; `chat_video` is also available for combined chat and video signaling. Chat/video use JSON message objects, video relays WebRTC `offer`, `answer`, and `candidate` messages, `json` accepts JSON values, `message` relays text, and `byte` relays binary data. Rooms can be selected with a `{room_id}` path parameter or `?room=...` query parameter. The `json`, `message`, and `byte` types can use fixed paths without a room ID.

```python
from lenapi import LENAPI, WebSocketService

app = LENAPI()
chat = WebSocketService(
    path="/ws/chat/{room_id}",
    type="chat",  # "video", "json", "message", or "byte"
    auth=authenticate,
    allowed_origins=["https://myapp.com"],
    max_message_size=64_000,
    max_connections=6,  # optional per-room limit
    max_total_connections=100,  # cap across all rooms in this process
    heartbeat_seconds=30,
)
app.include_router(chat.router())
```

The relay broadcasts each message to the connected clients in its room. For chat, send a JSON object such as `{"type": "chat.message", "text": "Hello"}`. The `json` type accepts JSON values on a fixed path such as `/ws/json`, without a room ID; its clients share the default channel. The heartbeat uses JSON `{"type":"lenapi.ping"}` and expects `{"type":"lenapi.pong"}`. Setting `allowed_origins` requires an `Origin` header and rejects missing or non-matching origins; leaving it as `None` accepts any origin, so configure an allowlist for browser-facing services. Origin checks do not authenticate clients: use `auth` for private services. `max_connections` caps one room; `max_total_connections` defaults to 100 connections across all rooms in this process (`None` disables that cap). These connection counters and the built-in room manager are process-local, so each worker enforces its own limit. Multi-worker deployments also need a shared pub/sub backend. Browser clients should use cookie-based auth or a custom WebSocket authenticator because the browser WebSocket API cannot set arbitrary request headers.

Create an invite ID with a normal HTTP endpoint, then share the WebSocket path with participants. The room is joined when the first client connects; the ID itself is not persisted by the service.

```python
from uuid import uuid4
from pydantic import BaseModel

class ChatMessage(BaseModel):
    type: str
    text: str

@app.post("/rooms")
def create_room():
    room_id = uuid4().hex
    return {"room_id": room_id, "websocket_path": f"/ws/chat/{room_id}"}

@app.post("/rooms/{room_id}/messages")
async def publish_message(room_id: str, data: ChatMessage):
    delivered = await chat.send(room_id, data.model_dump())
    return {"room_id": room_id, "delivered": delivered}
```

`WebSocketService.send(room_id, data)` sends to all clients currently connected
to that room and returns the delivery count. `WebSocketService.disconnect(room_id)`
closes every connection in the room. A POST route can call these methods, but
WebSocket `accept()` still belongs inside the WebSocket handshake handler.

Use the optional Faker service to generate realistic sample data. From the lenApi repository root, install it with `python -m pip install -e '.[faker]'`:

```python
from lenapi.service import Faker

fake = Faker("en_NG", seed=42)
print(fake.name())
print(fake.email())
```

The service delegates Faker providers such as `name()`, `email()`, and `address()` to a Faker instance. The seed makes an instance's generated sequence repeatable.

## OpenAI and Ollama services

Install the optional HTTP client with `python -m pip install -e '.[ai]'`. Both clients are async, return the provider's JSON response, and support async context management so connections close cleanly:

```python
import asyncio
from lenapi import OpenAI, Ollama

async def main():
    async with OpenAI(
        model="gpt-4.1-mini",
        base_url="https://api.openai.com/v1",
    ) as openai:
        answer = await openai.responses("Explain LenApi in one sentence.")
        print(answer.get("output_text", answer))

    async with Ollama(
        model="llama3.2",
        base_url="http://127.0.0.1:11434",
    ) as ollama:
        answer = await ollama.chat([
            {"role": "user", "content": "Explain LenApi in one sentence."},
        ])
        print(answer["message"]["content"])

asyncio.run(main())
```

`OpenAI` reads `OPENAI_API_KEY` from the environment unless `api_key=` is passed directly. Its `base_url` defaults to `https://api.openai.com/v1`; override it for an OpenAI-compatible endpoint. It supports the Responses API, chat completions, and embeddings. `Ollama` defaults to `http://127.0.0.1:11434`; set `base_url=` to use another Ollama host. Ollama must be running and the requested model must be available there. These calls are non-streaming and raise HTTPX errors for unsuccessful provider responses. The OpenAI Responses API is the recommended starting point for new projects; chat completions are included for compatibility. ([OpenAI API](https://platform.openai.com/docs/api-reference/responses), [Ollama chat API](https://docs.ollama.com/api/chat), [Ollama generate API](https://docs.ollama.com/api/generate))

## Payment provider plugins

Payment integrations are optional plugins with both synchronous and asynchronous APIs. Install their shared HTTPX transport with `python -m pip install -e '.[payments]'`. LenApi includes hosted-checkout adapters for Nomba, Flutterwave, Stripe, and Paystack; select only the provider your application uses. Credentials are read from environment variables by default, or can be passed explicitly:

```python
from lenapi import Payments

with Payments("paystack") as payment:  # PAYSTACK_SECRET_KEY
    checkout = payment.create_checkout(
        amount="2500.00", currency="NGN", email="customer@example.com",
        reference="order-2026-0001", success_url="https://example.com/payment/return",
    )
    print(checkout["data"]["authorization_url"])
    verified = payment.verify("order-2026-0001")
```

For `async def` routes, use `async with` and the `a`-prefixed methods:

```python
async with Payments("paystack") as payment:
    checkout = await payment.acreate_checkout(
        amount="2500.00", currency="NGN", email="customer@example.com",
        reference="order-2026-0001", success_url="https://example.com/payment/return",
    )
    verified = await payment.averify("order-2026-0001")
```

Provider environment variables: `NOMBA_CLIENT_ID`, `NOMBA_CLIENT_SECRET`, `NOMBA_ACCOUNT_ID`; `FLW_SECRET_KEY`; `STRIPE_SECRET_KEY`; and `PAYSTACK_SECRET_KEY`. For webhooks, configure `FLW_WEBHOOK_SECRET_HASH` and `STRIPE_WEBHOOK_SECRET` where relevant. Paystack and Stripe adapters expose `verify_webhook(raw_body, signature)`; Flutterwave accepts its `verif-hash` header as `signature`. Nomba's adapter obtains and refreshes its client-credentials token for checkout and verification.

Adapters use the provider's hosted checkout, so LenApi does not collect or store card numbers or CVVs. `create_checkout()` returns the provider response, and `verify()` queries the provider; your application must verify transaction status, amount, currency, and reference before fulfilling an order, and record its own order/payment state. Treat webhook deliveries as untrusted until signature validation and transaction verification succeed. Provider APIs have different capabilities and field requirements, so adapter-specific options are passed through to the provider. See the [Nomba checkout API](https://developer.nomba.com/nomba-api-reference/online-checkout/create-an-online-checkout-order), [Flutterwave verification and webhooks](https://developer.flutterwave.com/docs/transaction-verification), [Stripe Checkout Sessions](https://docs.stripe.com/api/checkout/sessions/create), and [Paystack transaction API](https://paystack.com/docs/api/transaction/).

You can also define a router without a prefix and mount it with the Django Ninja style `add_router(prefix, router)` call. Prefixes with or without trailing slashes are normalized:

```python
from lenapi import LENAPI, Router

app = LENAPI()
users = Router()

@users.get("/")
def list_users():
    return []

app.add_router("/users/", users)  # GET /users/
```

Assign a default Swagger tag to a router with `tag_name`. A tag on an individual route overrides the router tag. When neither is provided, the operation omits `tags` and Swagger UI uses its normal default grouping:

```python
auth = Router(prefix="/auth", tag_name="Authentication")

@auth.post("/login")
def login():
    return {"ok": True}  # tagged "Authentication"

@auth.get("/status", tag_name="Health")
def auth_status():
    return {"ok": True}  # route tag overrides the router tag

@auth.patch("/profile", tag_name="Authentication")
def update_profile():
    return {"updated": True}

app.include_router(auth)

@app.get("/", tag_name="General")
def home():
    return {"hello": "world"}
```

Add an IP-based cooldown to any route with `api_flag=True`. The client IP may call that route once per cooldown window; further requests receive `429 Too Many Requests` and a `Retry-After` header:

```python
@app.post("/webhooks/receive", api_flag=True, cool_time=300)
def receive_webhook(payload: dict):
    return {"received": True}
```

`cool_time` is in seconds and defaults to 300. IP cooldowns use the configured security backend. For request quotas, configure independent limits by scope:

```python
from lenapi import LENAPI, Router

app = LENAPI(throttle={
    "ip": "300/minute",                 # all requests, before authentication
    "anonymous": "30/minute",           # public calls and pre-auth requests by IP
    "authenticated": "600/minute",      # requests authenticated by route auth=
})

api = Router(prefix="/api")
# `authenticate` is your configured JWT, API-key, session, or other auth dependency.

@api.get("/public", throttle={"anonymous": "10/minute"})
def public_data():
    return {"ok": True}

@api.get("/account", auth=authenticate)
def account():
    return {"private": True}

app.include_router(api)
```

Rate strings accept seconds, minutes, hours, or days, for example `"10/second"`, `"60/minute"`, and `"1000/day"`. Throttling uses a fixed window that starts with the first request. `ip` counts every request from the client address before authentication. `anonymous` counts public calls and, on routes using `auth=`, all requests from that IP before credentials are verified. This intentionally includes successful logins so repeated password checks are stopped before they consume CPU. `authenticated` counts successful route-level authentication by a stable identity returned from `auth=`. Routes using `BasicAuth` receive a default `ip` limit of 10 requests per minute if no pre-auth `ip` or `anonymous` limit is configured. Set `throttle={}` to deliberately disable the inherited and default limit. Route and router `throttle=` values otherwise override inherited limits; an empty mapping disables inherited limits for that route or router. Endpoint-only auth declared with `Depends(...)` does not supply the identity to the authenticated quota; attach it with `auth=` when you want identity-based quotas.

When a quota is exceeded, LenAPI returns `429 Too Many Requests` with `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Remaining` headers. Quota counters use the same security backend as IP cooldowns. Choose `SECURITY_STORAGE = "database"` or `"redis"` in project `settings.py`. Database mode stores state in the internal `lenapi_security_store` table or MongoDB collection, initialized by migrations. Redis mode uses `REDIS_URL` and requires `pip install 'lenapi[redis]'` plus a running Redis server. The generated settings default to database storage and include `REDIS_URL = "redis://127.0.0.1:6379/0"` for switching to Redis. Shared database or Redis storage coordinates limits across workers. `SECURITY_STORAGE = "memory"` keeps counters in each process, so use it only for single-process development. LenAPI uses the ASGI connection's client address and does not trust forwarded-IP headers automatically; configure trusted proxy handling at your deployment edge before relying on IP quotas behind a proxy.

Model helpers include `model.EmailField()`, `model.IPField()`, `model.PhoneField()`, and `model.TextField()`:

```python
class Contact(model.Model):
    email = model.EmailField(unique=True)
    ip_address = model.IPField(nullable=True)
    phone = model.PhoneField(nullable=True)
    notes = model.TextField(nullable=True)
```

These declare database column types; validate request input with a Pydantic schema as well.

Model field helpers accept SQLAlchemy's `default=` option for per-field defaults, such as `count = model.IntegerField(default=0)` or `status = model.StringField(default="new")`. `model.BooleanField()` defaults to `True`; set `default=False` when a flag should start disabled. This default applies to every boolean field unless that field overrides it.

All model field declarations default to `nullable=True` and `blank=True`; pass `nullable=False` or `blank=False` to opt out. `blank` is recorded in the SQLAlchemy column's `info` metadata for form or schema tooling. Request-body requiredness is still controlled by the Pydantic request schema, so add a Pydantic default or optional type there when the API should accept an omitted or empty value.

Add a database index to any model field with `index=True`, for example `email = model.EmailField(index=True)`. Apply the schema change with `python lenora.py migration`; indexes are useful for fields that are frequently filtered or searched.

Numeric helpers include `model.BigIntField()` (also named `BigIntegerField()`), the existing `model.FloatField()`, and `model.DecimalField(max_digits=10, decimal_places=2)`. Decimal values use fixed precision and are represented as Python `Decimal` values:

```python
class Invoice(model.Model):
    quantity = model.BigIntField()
    weight = model.FloatField()
    amount = model.DecimalField(max_digits=12, decimal_places=2)
```

`model.UUIDField()` stores a native UUID and generates a UUID4 by default. `model.ImageField(upload_to="avatars")` stores a relative file path in the database; upload bytes are written beneath `MEDIA_ROOT` by `UploadFile.save()` or `UploadFile.save_image()`. The generated project setting `MEDIA_ROOT` defaults to `BASE_DIR / "media"`.

The repository root contains the lenApi library. Create an application scaffold and run it separately:

```bash
python -m pip install -e .
lenora startproject my_api
cd my_api
python lenora.py migration
python lenora.py run
```

Use the installed `lenora` command to create the first project; `python lenora.py ...` commands are available after entering the generated project. The server listens on `127.0.0.1:8009` by default. The generated application contains `app/models.py`, `app/schemas.py`, `settings.py`, `main.py`, a `tests/` package, a project-local CLI launcher, `alembic.ini`, and a `migrations/` folder. The default `User` model and authentication implementation live in the framework. The initial migration includes the framework's `User` table automatically; `LENAPI()` configures the authentication dependency without adding authentication routes. The framework repository contains the installable `lenapi` package; create an application scaffold separately with `lenora startproject`.

If `settings.py` is accidentally deleted, run `python lenora.py repair-settings` from the project directory. It recreates the generated defaults only when the file is missing and refuses to overwrite an existing settings file. The recreated file gets fresh `SECRET_KEY` and `API_KEY` values; review it before starting the app. The old `SECRET_KEY` cannot be recovered, so tokens signed with that key will no longer validate.

Run `python lenora.py apikey` inside a project to set `AUTHENTICATION = "api_key"`, add the `APIKey` model to `app/models.py` when it is missing, and generate/apply its migration. The command is safe to repeat; it leaves an existing model in place and applies no migration when the database is already current. The table stores each key's hash, owning user, active state, creation time, and revocation time; never store the raw key there.

`settings.py` centralizes the API title/version, docs and OpenAPI URLs, host, port, database selection, token settings, and shared security storage. `HOST = ""` uses `127.0.0.1`; set `HOST` directly to bind another address. `app = LENAPI()` loads project settings automatically. Each generated project gets a unique high-entropy `SECRET_KEY`; `TOKEN` uses it to sign tokens and configures token lifetime and issuer. Keep the key private. Changing it invalidates tokens signed with the old key. `DATABASE_TYPE` chooses one backend (`sqlite`, `mysql`, `postgresql`, or `mongodb`), and `DATABASE_URL` configures it. `ACTIVATE_ASYNC` enables asynchronous access using the same database URL. The default database is SQLite.

## Automated tests

Pytest is installed with lenApi, so you can run the framework test suite from the repository root:

```bash
python -m pytest
```

The `test`, `generate_test`, `run_test`, and `run_test_output` commands are application commands. Run them from a generated application directory, where `main.py` and `settings.py` live. `test` generates endpoint checks temporarily, runs them, and writes a CSV report under `outputs/`. `run_test_output` also writes a CSV report.

Request bodies are limited to 10 MiB by default, and lenApi allows up to 30 seconds to receive the body. Configure `MAX_REQUEST_BODY_SIZE` (bytes) and `REQUEST_BODY_TIMEOUT_SECONDS` in `settings.py`; set either to `None` to disable that limit. Oversized bodies receive HTTP 413, and bodies that exceed the receive timeout receive HTTP 408. Direct `Application(...)` instances accept the equivalent `max_request_body_size` and `request_body_timeout` arguments.

`LENAPI()` uses values from `settings.py` when an override is omitted. Pass `None` explicitly to disable an optional feature or limit, for example `LENAPI(docs_url=None, max_request_body_size=None)`. The public `UNSET` sentinel is available for your own function defaults when omitted and explicit `None` need different meanings:

```python
from lenapi import UNSET

def update_field(value=UNSET):
    if value is UNSET:
        return "leave the stored value unchanged"
    return value  # None is an explicit value and can clear a nullable field.
```

For Pydantic request bodies, use `model_dump(exclude_unset=True)` for partial updates; Pydantic already tracks which fields the client sent.

The `run` command looks for `app` in the current application's `main.py`. It watches application files and reloads the server when code changes. The default port is `8009`. Change `PORT` in application `settings.py` or pass `--port` to `run`, for example `python lenora.py run --port 8010`. Host and app can also be changed with `--host` and `--app package.module:variable`; disable reload with `--no-reload`. Installing the package also provides the `lenora run` command. The former `runserver` spelling remains available as a compatibility alias.

## Async database access

Routes can be `async def`, and lenApi can inject a SQLAlchemy `AsyncSession` when configured with an `AsyncDatabase`. The standard lenApi installation includes SQLAlchemy's `asyncio` extra (including `greenlet`) and `aiosqlite`, so async SQLite is ready to use. For async PostgreSQL or MySQL, install the corresponding optional driver extra.

Set `ACTIVATE_ASYNC = True` in `settings.py` to enable awaited database requests alongside synchronous access. Both use `DATABASE_URL`; LenApi selects the async SQLAlchemy driver automatically. SQLite is ready by default. For MySQL install `pip install 'lenapi[mysql,async-mysql]'`; for PostgreSQL install `pip install 'lenapi[postgres,async-postgres]'`.

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lenapi import LENAPI
from app.models import Student

app = LENAPI()

@app.get("/async-students")
async def list_students(db: AsyncSession):
    result = await db.scalars(select(Student))
    return result.all()
```

`AsyncSession` is opened for the request and closed afterwards. Async model helpers use that request session automatically; pass `session=db` when calling them outside a request:

```python
student = await Student.acreate(person_name="Ada", course="Python")
student = await Student.aget(id=student.id)
student.course = "Databases"
await student.asave()

students = await Student.afilter(course="Databases").order("_id").all()
deleted_count = await Student.adelete(id=student.id)
```

`Model.adelete(...)` requires filters to prevent accidental full-table deletes. You can also call `await instance.adelete()` or `await Model.afilter(...).delete()`. The synchronous `Model.get()`, `.filter()`, and `.save()` methods remain available. PostgreSQL and MySQL drivers are available through the `async-postgres` and `async-mysql` extras, alongside their existing synchronous driver extras for migrations.

## Middleware

Add ASGI middleware with `app.add_middleware`. Middleware is applied in registration order, with the first middleware added wrapping the rest:

```python
from lenapi import LENAPI
from lenapi.security import authentication
from lenapi.middleware import (
    CORSMiddleware,
    DocsAuthMiddleware,
    GZipMiddleware,
    RequestIDMiddleware,
    TimingMiddleware,
)
import settings

app = LENAPI()
app.add_middleware(RequestIDMiddleware)
app.add_middleware(TimingMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://frontend.example"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["authorization", "content-type"],
)

# Optional: protect only Swagger and OpenAPI.
docs_auth = authentication("basic_login", settings=settings, app=app)
app.add_middleware(
    DocsAuthMiddleware,
    auth=docs_auth,
    paths=(app.docs_url, app.openapi_url),
    session_factory=app.session_factory,
    async_session_factory=app.async_session_factory,
)
```

`GZipMiddleware` uses Python's standard library, compresses eligible response types when the request accepts gzip, and streams responses after buffering up to `minimum_size` (500 bytes by default). It adds `Content-Encoding: gzip` and `Vary: Accept-Encoding`, skips already-encoded and partial responses, and supports `compresslevel` from 0 to 9. It does not require Starlette. Custom middleware follows the ASGI callable contract: `Middleware(app, **options)` and `await app(scope, receive, send)`. Request IDs and elapsed time are returned in `X-Request-ID` and `X-Process-Time-Ms`.

## Database transactions

Use a database transaction context when multiple writes must commit or roll back together. Use the yielded SQLAlchemy session for every operation in that block:

```python
with app.database.transaction() as db:
    db.add(first_record)
    db.add(second_record)
```

For async work, use `async with app.async_database.transaction() as db:` and await each SQLAlchemy operation. The context commits on success, rolls back if an exception escapes, and closes the session.

## Cache service

`app.cache` is an async JSON-value cache. Set `CACHE_BACKEND` in `settings.py` to `"memory"` (default), `"database"`, or `"redis"`. SQL database-backed cache uses the shared `lenapi_security_store` table, so run `python lenora.py migration` first. Redis mode uses `REDIS_URL` and requires `pip install '.[redis]'`.

```python
await app.cache.set("profile:42", {"name": "Ada"}, ttl=300)
profile = await app.cache.get("profile:42")
await app.cache.delete("profile:42")
```

## Durable background jobs

`BackgroundTasks` remains useful for short in-process work after a response. For a worker queue, `BackgroundQueue` supports `database`, `redis`, or `cache`/`memory` backends. The database queue table is excluded from ordinary migrations. Export a `queue` or `background_queue` from `main.py`, then run `python lenora.py background-worker`; it creates and applies the queue migration before starting the worker. Redis mode requires `pip install '.[redis]'` and Redis persistence configured for the durability you need. The cache/memory backend is process-local and ephemeral: queued jobs are lost when the process exits and cannot be shared with a separate worker process. Jobs on database and Redis backends are delivered at least once; write task handlers to be idempotent.

```python
from lenapi import BackgroundQueue

# Database backend:
queue = BackgroundQueue(app.database)
# Redis backend: queue = BackgroundQueue(backend="redis", redis_url=settings.REDIS_URL)
# Process cache: queue = BackgroundQueue(backend="cache")

@queue.task("send_welcome")
async def send_welcome(email: str):
    ...

job_id = await queue.enqueue("send_welcome", "ada@example.com")
```

Export the queue from `main.py` with its task registrations, then start it in a separate process:

```bash
python lenora.py background-worker
```

The API process and worker must register the same task names and handlers. `run_once()` is available for one-job execution in tests or custom worker loops; `status(job_id)` returns the persisted job status.

Swagger UI is available at [http://127.0.0.1:8009/docs/](http://127.0.0.1:8009/docs/), and the generated OpenAPI document is at [http://127.0.0.1:8009/openapi.json](http://127.0.0.1:8009/openapi.json). These are enabled by default. Set `API_TITLE`, `API_VERSION`, `DOCS_URL`, and `OPENAPI_URL` in `settings.py` to configure the Swagger metadata and paths, or override them directly with `LENAPI(title="My API", version="1.0.0", docs_url="/docs/", openapi_url="/openapi.json")`.

Protect both the Swagger page and its OpenAPI JSON with `DocsAuthMiddleware`. It accepts the same callable authentication dependencies used by routes; pass the configured docs paths so customized URLs are protected too:

```python
from lenapi import LENAPI, DocsAuthMiddleware
from lenapi.security import authentication

app = LENAPI()
docs_auth = authentication("basic_login")
app.add_middleware(
    DocsAuthMiddleware,
    auth=docs_auth,
    paths=(app.docs_url, app.openapi_url),
    session_factory=app.session_factory,
    async_session_factory=app.async_session_factory,
)
```

The session factories let database-backed authentication such as Basic or API-key auth query the configured user model. This middleware only protects the documentation endpoints; it does not secure API routes. Omit it to keep the default public docs, or pass another authentication dependency such as JWT or API-key auth.

For the built-in docs login, set `SAFE_GUARD_DOCS_URL = True` in `settings.py`. The app then serves an administrator sign-in page at `/`, redirects unauthenticated requests for `/docs/` or `/openapi.json` to it, and redirects successful logins to Swagger. Failed sign-ins are limited to 5 per client IP in a 300-second window by default; configure `DOCS_LOGIN_MAX_ATTEMPTS` and `DOCS_LOGIN_WINDOW_SECONDS` to adjust this. With `SECURITY_STORAGE = "database"` or `"redis"`, the limiter uses shared storage and coordinates across workers; `"memory"` keeps counters per process and is intended for single-process development. Create an administrator with `python lenora.py admin` after `python lenora.py migration`; the password prompt is hidden and the framework stores only a password hash. The framework's `AdminUser` and expiring session storage are created by the first migration. Admin records and sessions use the selected application database, including MongoDB; Redis is used only for attempt counters when selected. The session cookie is HttpOnly and expires after `DOCS_SESSION_LIFETIME_SECONDS` (default 3600 seconds). `DOCS_COOKIE_SECURE` follows the ASGI scheme by default; set it to `True` when TLS terminates at a reverse proxy.

The docs gate only handles `/`, `/logout`, the configured Swagger path, and the configured OpenAPI path. Regular application routes do not query the admin or session tables; they pass through a small path check. Opening protected Swagger or OpenAPI does validate the session against the configured database, which is the expected cost of revocable server-side sessions.

To remove the gate, set `SAFE_GUARD_DOCS_URL = False` and restart the app. The admin tables may remain in the database; they are not queried while the feature is disabled.

The sample app exposes the model queries as HTTP routes: `POST /classrooms` creates a classroom, `GET /classrooms` lists them, and `POST /students` accepts an optional `classroom_id`. `POST /students/batch` creates multiple students, `PUT /students/batch` updates multiple students by ID, and `DELETE /students/batch` deletes multiple students by ID. Batch requests are all-or-nothing: unknown IDs, duplicate names, or a protected deletion reject the whole operation. `PUT /students/{student_id}` updates one student; it returns HTTP 404 if the ID does not exist. `GET /students/{student_id}/classroom` returns the related classroom. Other student routes list, search, and check existence. These routes appear in Swagger at `/docs/`.

Batch request examples:

```json
{"students": [
  {"person_name": "Ada", "course": "Python"},
  {"person_name": "Grace", "course": "Databases"}
]}
```

Use the same `students` wrapper for batch updates, with an `id` on each item. Batch deletion accepts `{"ids": [1, 2]}`.

## Dependencies

Use `Depends` to share logic across routes. A dependency can accept the same typed path/query parameters and `Request` object as a route, and may be synchronous or asynchronous:

```python
from lenapi import Application, Depends

app = Application()

def current_user(user_id: int):
    # Replace this with real authentication logic.
    return {"id": user_id, "name": "Ada"}

@app.get("/profile")
def profile(user: dict = Depends(current_user)):
    return user
```

Dependencies are cached once per request, so if multiple parameters use the same dependency callable, it runs once. Synchronous routes, dependencies, authentication and token-lookup callbacks, HMAC nonce-store methods, and `DocsAuthMiddleware` authenticators run in a worker thread so blocking I/O in them does not block the ASGI event loop. Async callbacks run on the event loop; use async I/O there or explicitly offload blocking work yourself.

## JWT bearer authentication

`JWTAuth` signs and verifies HS256 access tokens and works as a route dependency. It validates the signature, expiration, optional not-before time, and configured issuer. To add credential and refresh endpoints, mount the router with your own credential checker. The checker must validate the password (using a password hash in a real app) and return a string user ID or `(user_id, claims)`; invalid credentials return `None`:

```python
def authenticate_user(username: str, password: str):
    user = find_user(username)
    if user is None or not verify_password(password, user.password):
        return None
    return str(user.id), {"role": user.role}

app.include_router(jwt_auth.router(authenticate_user))
```

This adds `POST /auth/token` with `{"username": "...", "password": "..."}` and `POST /auth/refresh` with `{"refresh_token": "..."}`. Both routes have a default IP-based limit of 10 requests per minute, including failed credential checks. To set a different limit, pass `throttle={"ip": "5/minute"}` to `jwt_auth.router(authenticate_user, throttle=...)`; pass `throttle={}` only when you intentionally want to disable the built-in limit. In production, select shared `SECURITY_STORAGE = "database"` or `"redis"` so refresh-token replay protection and limits work across workers. Each refresh token is one-time-use: the first refresh returns a replacement refresh token, while a replay is rejected. The in-memory fallback protects only within one process. Refresh tokens issued before one-time-use identifiers were added must be replaced by signing in again. Both routes return an `access_token` and `refresh_token`; access tokens expire after `TOKEN["LIFETIME_SECONDS"]` and refresh tokens after `TOKEN["REFRESH_LIFETIME_SECONDS"]`. Refresh tokens are rejected by protected routes.

`JWTAuth` does not authenticate usernames or passwords itself; your callback owns that check. Keep a high-entropy secret private and use HTTPS in deployed applications:

Choose the application's default authentication in the project `settings.py`:

```python
AUTHENTICATION = "basic_login"  # choose one: jwt, api_key, basic_login, opaque_bearer, or session
BASIC_AUTH_FIELD = "email"  # username, email, phone, or another User field
```

The HTTP Basic username slot is treated as the configured identifier, so a client can send `auth=(email, password)` when `BASIC_AUTH_FIELD = "email"`. The framework's default `User` model supports `username` and `email` as identifiers.

`LENAPI()` reads this setting and configures the framework's default `User` model and authentication dependency. It does not register built-in `/auth/register`, `/auth/profile`, `/auth/token`, or `/auth/refresh` routes. Create only the auth endpoints your application needs, and use `app.authenticator` on protected routes:

```python
from lenapi import Depends

@app.get("/protected")
def protected(user=Depends(app.authenticator)):
    return {"authenticated": True}
```

You can protect an entire router with `auth=`, or protect an individual endpoint with the same option. A route-level authenticator overrides the router's default. `authentication(type="jwt")` uses the current project's `settings.py` and built-in `User` model by default; pass `settings=`, `user_model=`, or `app=` to customize it. Authenticated endpoints can also receive the identity with `Depends(authenticate)`:

```python
from pydantic import BaseModel
from lenapi import LENAPI, Router
from lenapi.security import authentication

app = LENAPI()
authenticate = authentication(type="jwt")
api = Router(prefix="/api")

class Greeting(BaseModel):
    name: str

@api.post("/hello", auth=authenticate)
async def hello(data: Greeting):
    return {"message": f"Hello, {data.name}!"}

app.include_router(api)
```

To apply the same requirement to every route in a group, initialize it with `Router(prefix="/api", auth=authenticate)`. Route authentication is enforced before the endpoint runs and is included in the generated OpenAPI security metadata.

The primary authentication setting chooses one method for the scaffold's default protected route: `jwt`, `api_key`, `basic_login`, `opaque_bearer`, or `session`. Session cookies, HMAC, and OIDC can also be attached as additional callable dependencies on selected routes. This lets an app use API keys generally and require a session cookie on a browser route, or require both on one route:

```python
from lenapi import Depends
from lenapi.security import hmac_auth, oidc_auth, opaque_bearer_auth, session_auth

session = session_auth(store=app.security_store, cookie_name="session_id")
stored_token = opaque_bearer_auth(store=app.security_store)
signed_request = hmac_auth(settings.HMAC_SECRET, nonce_store=app.security_store)
provider_token = oidc_auth(oidc_token_lookup)

@app.get("/browser/private")
def browser_private(api_identity=Depends(auth), session_identity=Depends(session)):
    return {"api_user": api_identity, "session_user": session_identity}

@app.get("/browser/session-only")
def browser_session_only(user=Depends(session)):
    return {"user": user}

@app.post("/webhooks/provider")
async def provider_webhook(identity=Depends(signed_request)):
    return {"accepted": True}

@app.get("/federated/private")
def federated_private(identity=Depends(provider_token)):
    return {"identity": identity}
```

`SECURITY_STORAGE` selects the shared store used by IP flags, sessions, opaque bearer tokens, and HMAC nonce replay checks. The store exposes async `get(key)`, `set(key, value, ttl=...)`, `delete(key)`, and atomic `claim(key, ttl)` methods. For a custom login flow, create a random session ID, store the JSON-serialized identity as `await app.security_store.set(f"session:{session_id}", json.dumps(identity), ttl=3600)`, and set it in a cookie with `Response.set_cookie(..., httponly=True, secure=True, samesite="lax")`. Opaque bearer tokens can be stored similarly under `opaque_bearer:{token}`. `oidc_token_lookup(token)` must validate the provider's issuer, audience, signature, and expiry using its supported OIDC library.

## Social login

`SocialAuth` generates provider login and callback routes for Google, GitHub, and LinkedIn. X and Facebook are temporarily disabled; if either is listed in `providers`, it is ignored, so no login or callback route is generated for it. Other unknown provider names remain configuration errors. Configure each available provider's client ID, client secret, and exact callback URI in the generated `settings.py` (or load them from environment variables). The generated settings leave credentials blank. Register each callback URI with the matching provider.

```python
# settings.py — replace with the exact origin serving your frontend.
SOCIAL_AUTH_ALLOWED_ORIGINS = ["https://myapp.com"]

# main.py
from lenapi import LENAPI
from lenapi.security import SocialAuth

auth = SocialAuth(
    providers=["google", "github", "linkedin"],
    auto_register=True,
    jwt_enabled=True,
)

app = LENAPI()
app.include_router(auth.router(), prefix="/auth")
```

This mounts routes such as `/auth/google/login` and `/auth/google/callback` only on the app that includes the router. The callback exchanges the provider code, validates the OAuth state (and PKCE where supported), fetches the provider profile, and returns a LenApi bearer token when `jwt_enabled=True`. It does not return the provider's access token.

`allowed_origins` can also be passed directly to `SocialAuth`; otherwise it reads `SOCIAL_AUTH_ALLOWED_ORIGINS` from `settings.py`. When a browser sends an `Origin` header to a provider login route, LenApi requires an exact match in this list and returns `403` otherwise. Replace `https://myapp.com` with the exact origin serving your frontend. Top-level navigations can omit `Origin`; those still rely on the required, random, browser-cookie-bound OAuth `state` check on callback. Do not treat the origin allowlist as a replacement for OAuth state validation, HTTPS, or correct provider callback URL registration. The provider callback itself is not checked against the browser `Origin` header because providers may omit it or send a different origin.

With `auto_register=True`, LenApi creates a framework `User` (or an `app.models.User` subclass) on the first successful provider login and stores the provider subject mapping in the framework-managed `lenapi_social_accounts` table. Run `python lenora.py migration` and `python lenora.py migrate` before first use. Only verified provider emails are copied to the local user. If a verified email is already attached to a different account, sign-in returns `409`; LenApi will not silently link accounts by email.

The scaffold includes blank settings for `GOOGLE_*`, `GITHUB_*`, and `LINKEDIN_*` client ID, client secret, redirect URI, and cookie security values. Keep client secrets in environment variables or a local ignored `.env` file.

Use `SocialAuth` for the built-in Google, GitHub, and LinkedIn authorization-code flows; retain a custom flow when your provider or account-linking rules need different behavior.

For HMAC, configure `HMAC_SECRET` with at least 32 bytes, then clients can generate the header signature with `hmac_request_signature(secret, method, path, timestamp, body, nonce=nonce)` and send it with `X-LenAPI-Timestamp`, `X-LenAPI-Nonce`, and `X-LenAPI-Signature`. Signatures older than the configured tolerance and repeated nonces are rejected. The replay cache is process-local; multi-worker deployments should replace it with a shared nonce store.

HMAC mode signs the request timestamp, method, path plus query, body digest, and a unique nonce. Configure `HMAC_SECRET` with at least 32 bytes, then clients can generate the header signature with `hmac_request_signature(secret, method, path, timestamp, body, nonce=nonce)` and send it with `X-LenAPI-Timestamp` and `X-LenAPI-Nonce`. Signatures older than the configured tolerance and repeated nonces are rejected. The replay cache is process-local; multi-worker deployments should replace it with a shared nonce store.

## Authorization

Authentication confirms who called the API; authorization checks what they may do. Use the reusable dependencies for roles, permissions, and ownership checks. The auth dependency must return identity claims, such as a JWT `role`/`permissions`, or a session/API-key mapping with a user ID.

```python
from lenapi import Depends, require_permission, require_role

admin_access = require_role("admin", auth=auth)
write_students = require_permission("students:write", auth=auth)

@app.delete("/admin/reports")
def delete_reports(identity=Depends(admin_access)):
    return {"deleted": True}

@app.post("/students")
def create_student(identity=Depends(write_students)):
    return {"created_by": identity["sub"]}
```

For object-level authorization, load the requested row and check that its owner matches the authenticated user's `sub`, `user_id`, or `id`. The dependency returns the row on success and returns `404` when the row is missing or belongs to someone else:

```python
from lenapi import Depends, require_owner

document_access = require_owner(
    lambda document_id: Document.get(id=int(document_id)).get(),
    auth=auth,
    path_param="document_id",
    owner_field="user_id",
)

@app.get("/documents/{document_id}")
def read_document(document=Depends(document_access)):
    return document
```

### CSRF protection for cookie-authenticated routes

Create a `CSRFProtect` instance with a secret of at least 32 bytes. Provide its signed token in a readable cookie, then require the same token in `X-CSRF-Token` on write requests. Apply it alongside authentication to routes that accept browser cookies:

```python
from lenapi import Depends, JSONResponse
from lenapi.security import CSRFProtect

csrf = CSRFProtect(settings.SECRET_KEY, secure=True, samesite="lax")

@app.get("/auth/csrf")
def issue_csrf_token():
    response = JSONResponse({"message": "CSRF token issued"})
    csrf.set_cookie(response)
    return response

@app.post("/account/email")
def change_email(user=Depends(auth), _csrf=Depends(csrf)):
    return {"updated": True}
```

Safe methods do not require the header; state-changing methods do. `SameSite` is additional protection and does not replace this token check. Use HTTPS so the CSRF cookie can be `Secure`.

`API_KEY` is generated uniquely for each new project. The scaffold's Basic/JWT credential callback looks up `User` in the database and verifies its hashed `password`. Use `make_password(password)` to hash a password and `check_password(password, stored_password)` to verify it; the latter returns a boolean. Never store plaintext passwords. The older `hash_password()` and `verify_password()` names remain available as aliases. Use HTTPS for Basic credentials and bearer/API key requests outside local development.

The framework provides `lenapi.User` with `username`, nullable `email`, `first_name`, `last_name`, `password`, and `api_key` fields. The default `UserRegistration` request asks for `username`, `first_name`, `last_name`, `password`, and `confirm_password`; email is not required or included in the default registration response. Apps that want to collect email can define a custom registration schema and pass its email value through `register_user()`. When a trusted provisioning flow creates a user directly, store the password hash:

```python
from lenapi import User
from lenapi.security import make_password

# In a registration handler, after validating the submitted password:
user = User.create(
    username=username,
    email=email,
    first_name="",
    last_name="",
    password=make_password(password),
)
```

Project-specific database models belong in `app/models.py`; the default framework `User` model is maintained by lenApi and included in the first migration. Authentication is configured as a dependency; the framework does not add login, registration, refresh, or profile routes automatically.

To add project-specific fields to the existing user table, subclass the framework model in `app/models.py`. LenApi treats subclasses of concrete `Model` classes as single-table extensions, so the subclass reuses the parent's table and Alembic detects the extra columns. `server_default` gives existing database rows values when a non-null column is added:

```python
from sqlalchemy import false

from lenapi import model
from lenapi.models import User as BaseUser


class User(BaseUser):
    bio = model.TextField(default="", nullable=True)
    is_verified = model.BooleanField(
        default=False, nullable=False, server_default=false()
    )
```

Import the extended `User` from `app.models` in application code and run `python lenora.py migration`. Do not set a second table name for this subclass; it extends the framework's existing `user` table. `bio` is nullable because MySQL does not support defaults on `TEXT` columns; if existing rows need an empty string instead of `NULL`, add a data backfill to the generated migration. Ordinary subclasses of the abstract `Model` base still get a table inferred from their class name.

When `settings.AUTHENTICATION = "api_key"`, registration generates a URL-safe API key with 32 bytes of cryptographically secure randomness and returns it once as plain text in the registration response. The `user.api_key` column remains nullable for installations using another authentication mode; in API-key mode, newly registered users get a key automatically. lenApi stores only the key's SHA-256 digest in that column, so the original key cannot be recovered later. Send the returned key on authenticated requests with `X-API-Key: <key>`. The API-key authenticator hashes the supplied key before lookup and returns that user's identity. Existing users need to be issued a key through a key-rotation/provisioning flow before they can use API-key authentication.

Use `register_user()` from your own registration handler if you want the framework's user creation and password handling. The password encoding uses Django's `pbkdf2_sha256` format, so Django's password hashers can verify it; this is one-way hashing, not reversible encryption. See [Django password management](https://docs.djangoproject.com/en/6.0/topics/auth/passwords/) for the encoded format and verification helpers.

### Cookie-based browser sessions

lenApi routes can return JSON and set one or more cookies on the same response. The browser sends those cookies on later requests, which you can read with `request.cookies`:

```python
from lenapi import Depends, HTTPException, JSONResponse, Request

@app.post("/auth/cookie-login")
def cookie_login(data: LoginRequest):
    user_id = authenticate_user(data.username, data.password)
    if user_id is None:
        raise HTTPException(401, "Invalid username or password")

    response = JSONResponse({"message": "Signed in"})
    response.set_cookie(
        "access_token",
        authenticator.create_access_token(user_id),
        max_age=1800,
        httponly=True,
        secure=True,  # Use HTTPS in deployed environments.
        samesite="lax",
        path="/",
    )
    return response

def current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401, "Sign in required")
    try:
        return authenticator.decode_token(token)
    except ValueError as exc:
        raise HTTPException(401, "Invalid or expired session") from exc

@app.get("/account")
def account(user=Depends(current_user)):
    return {"user_id": user["sub"]}

@app.post("/auth/logout")
def logout():
    response = JSONResponse({"message": "Signed out"})
    response.delete_cookie("access_token", path="/")
    return response
```

Cookies are convenient for browser clients; native apps and server-to-server clients can keep using `Authorization: Bearer ...`. For cookie authentication, use HTTPS, `HttpOnly`, and an appropriate `SameSite` setting, and add CSRF protection for state-changing browser requests. If your frontend is on another origin, configure CORS to allow credentials and use `credentials: "include"` in browser fetch calls.

```python
import os

from lenapi import Depends
from lenapi.security import JWTAuth

jwt_auth = JWTAuth(
    secret_key=os.environ["LENAPI_JWT_SECRET"],
    token_lifetime_seconds=1800,
    refresh_token_lifetime_seconds=2592000,
    issuer="my-api",
)

# After checking the user's password in your login handler:
access_token = jwt_auth.create_access_token(
    str(user.id),
    claims={"role": user.role},
)

@app.get("/account")
def account(claims: dict = Depends(jwt_auth)):
    return {"user_id": claims["sub"], "role": claims.get("role")}
```

Send the token with `Authorization: Bearer <access_token>`. Missing, invalid, or expired tokens receive HTTP 401. Protected routes also get a Bearer authorization scheme in the generated OpenAPI document, so Swagger UI displays its **Authorize** control.

Use `Body()` with `Any` when an endpoint should accept and return arbitrary JSON without Pydantic validation:

```python
from typing import Any
from lenapi import Body

@app.post("/webhooks/events")
async def receive_event(payload: Any = Body()):
    return payload
```

Declare an upload as `image: UploadFile = File(...)`. The `File(...)` marker means required; LenAPI parses the multipart request and generates Swagger's file chooser from the annotation. `save("photos")` resolves to `MEDIA_ROOT/photos`, validates image content, creates a safe generated name, and returns that stored name. Set `keep_filename=True` to keep the sanitized uploaded name; an existing file is never overwritten. Use `list[UploadFile] = File(...)` to accept multiple files:

```python
from lenapi import File, UploadFile, model
import settings

class Photo(model.Model):
    image = model.ImageField(upload_to="photos")

@app.post("/photos")
async def upload_photo(image: UploadFile = File(...)):
    filename = await image.save("photos", keep_filename=True)
    photo = Photo(image=f"photos/{filename}")
    photo.save()
    return {"id": photo.id, "image": photo.image}
```

For other file types, use `await file.save("documents", type="pdf")` or `type="doc"` (`.doc` and `.docx`). `type=None` allows any file type. Selected PDF and Word types are checked against their file signatures; all saves sanitize names and refuse to overwrite existing files. `save_image(directory)` remains available for explicit absolute-directory image saves. Use `Request.form()` for custom form parsing; typed `UploadFile` route arguments automatically describe the multipart schema in OpenAPI.

The stored path is relative. `LENAPI` serves files from `MEDIA_ROOT` under `MEDIA_URL` (default `/media/`), so a saved path such as `photos/<filename>.jpg` is available at `/media/photos/<filename>.jpg`. Set both `MEDIA_ROOT` and `MEDIA_URL` in `settings.py`; media paths are restricted to files inside `MEDIA_ROOT`, and directory listings are not exposed. Request body size limits apply to multipart uploads too.

## Webhooks and background work

Webhook senders retry deliveries, so verify the provider signature and deduplicate with its event ID. `webhook_auth()` verifies an HMAC-SHA256 signature over the exact raw body. If a timestamp header is configured, it verifies freshness and signs `timestamp + b"." + body`. Give the dependency the shared security store to acknowledge duplicate event IDs with HTTP 200 without running the route handler again:

```python
from typing import Any
from lenapi import BackgroundTasks, Body, Depends
from lenapi.security import webhook_auth

webhook = webhook_auth(
    settings.WEBHOOK_SECRET,
    security_store=app.security_store,
    provider="payments",
    signature_header="x-webhook-signature",
    event_id_header="x-webhook-id",
    timestamp_header="x-webhook-timestamp",
)

def process_event(event_id, payload):
    # Process the event; open a fresh database session here if needed.
    ...

@app.post("/webhooks/payments")
async def payments_webhook(
    payload: Any = Body(),
    verified=Depends(webhook),
    background_tasks: BackgroundTasks = None,
):
    background_tasks.add_task(process_event, verified["event_id"], payload)
    return {"received": True}
```

`BackgroundTasks.add_task(function, *args, **kwargs)` runs work after lenApi sends the response. Synchronous functions run in a worker thread; async functions are awaited. This built-in runner is in-process and best for short follow-up work. A process crash can lose queued work, so use a durable queue for jobs that must survive restarts. The webhook dependency claims an event ID for seven days by default; configure `deduplication_ttl` if the provider retries for a different period. If the security store is database-backed, run `python lenora.py migration` first.

## Databases

`Application()` uses SQLite by default and creates the `lenapi.db` file in the current working directory. Tables are managed by migrations, not created automatically at startup. `Database` accepts a SQLAlchemy URL when you want a different relational database. SQLAlchemy-backed models support SQLite, MySQL, and PostgreSQL. MongoDB is available through a separate optional Pydantic document API; it does not use SQL migrations or the SQLAlchemy `Model` base. Install optional drivers with `pip install '.[mysql]'`, `pip install '.[postgres]'`, or `pip install '.[mongodb]'`.

### MongoDB documents

Install the official PyMongo driver extra with `python -m pip install -e '.[mongodb]'`. Create a `MongoDatabase` using a MongoDB URI and database name, then bind Pydantic document classes to it. The default collection name is the lower-case document class name; set `collection_name` to override it. MongoDB creates collections on first write; `migration` can also pre-create empty collections for imported `MongoDocument` classes. Document field changes do not use Alembic schema revisions.

```python
from lenapi import LENAPI, MongoDatabase, MongoDocument

mongo = MongoDatabase(
    "mongodb://127.0.0.1:27017",
    "school",
    serverSelectionTimeoutMS=5000,
)

class Student(MongoDocument):
    name: str
    course: str
    collection_name = "students"

Student.bind(mongo)
app = LENAPI(mongodb=mongo)

# Synchronous CRUD
student = Student.create(name="Ifeanyi", course="Python")
found = Student.get(id=student.id)
students = Student.filter(course="Python")
student.course = "MongoDB"
student.save()
student.delete()

# Async CRUD
student = await Student.acreate(name="Ada", course="Python")
found = await Student.aget(id=student.id)
students = await Student.afilter(course="Python")
await student.asave()
await student.adelete()
```

`MongoDatabase.ping()` and `await MongoDatabase.aping()` check connectivity. To select MongoDB, set `DATABASE_TYPE = "mongodb"` and set `DATABASE_URL` to a Mongo URI whose path contains the database name, such as `mongodb://localhost:27017/my_database`. `ACTIVATE_ASYNC` controls whether LenApi also creates PyMongo's async client. `migration` and `migrate` ensure collections exist for imported `MongoDocument` models and framework security/admin state; document fields are schema-flexible and do not use Alembic revisions. `SECURITY_STORAGE = "database"` uses MongoDB for shared security state, while Redis and process memory remain optional choices. MongoDB uses the official synchronous and `AsyncMongoClient` APIs; see the [PyMongo driver guide](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/).

Put application models in `app/models.py` and import that module from `main.py`. The migration command loads `main:app`, so it discovers those model classes before creating a migration. The library's `Model` base supplies an integer `id`:

```python
from lenapi import User, model

class Classroom(model.Model):
    name = model.StringField(max_length=120)

class Student(model.Model):
    person_name = model.StringField(max_length=120, unique=True)
    course = model.StringField(max_length=120)
    classroom_id = model.ForeignKeyField("classroom.id", nullable=True, model=model.null)

class Course(model.Model):
    name = model.StringField(max_length=120)

class Teacher(model.Model):
    name = model.StringField(max_length=120)
    courses = model.ManyToManyField("Course")

class Verification(model.Model):
    user_id = model.ForeignKeyField("user.id", model=model.cascade.delete)

class AuditLog(model.Model):
    user_id = model.ForeignKeyField("user.id", nullable=True, model=model.null)

class Invoice(model.Model):
    user_id = model.ForeignKeyField("user.id", model=model.protected)
```

Table names are inferred from model class names in lowercase (`Student` becomes `student`); no table-name declaration is needed. Import `model` from `lenapi` and access the model base class, fields, and relationship behaviors through it, such as `model.Model`, `model.StringField`, and `model.ForeignKeyField`.

From the project root, run one command to detect model changes, write a migration under `migrations/versions/`, and apply it to the configured database:

```bash
python lenora.py migration
```

After changing models, run the same command again; it creates and applies the next migration. Use `--message "describe change"` to name a migration. Existing unversioned SQLite databases that already match the models are adopted without changing their data. Inside a request, models provide these query helpers:

Inspect migration state with `python lenora.py showmigrations`; it lists each local revision, whether it is applied, and database revision IDs whose files are missing. If the database references a missing revision, `python lenora.py repair-migrations` checks that the live schema exactly matches the current models before stamping the database to the local head. `python lenora.py delete_migrations` prompts for y/n confirmation, then drops all tables and views in the configured database and removes local migration revision files. It is destructive and intended only for a full reset. `python lenora.py clear_migrations` remains limited to an empty database.

```python
student = Student.create(person_name="Ifeanyi", course="Python")
students = Student.get.all()
specific_student = Student.get(person_name="Ifeanyi").get()
exists = Student.get(person_name="Ifeanyi").exists()
name = Student.get.unique("person_name").get(person_name="Ifeanyi")
summary = Student.get("person_name", "course").get(person_name="Ifeanyi")
matching_students = Student.filter(course="Python").values().order("_id").all()
searched_students = Student.search(person_name="ifea").all()
matching_name_or_course = Student.search_any("python", "person_name", "course").all()
first_page = Student.filter(course="Python").all(limit=20, offset=0)
second_page = Student.filter(course="Python").limit(20).offset(20).all()
latest_students = Student.get.all(limit=20, offset=40)
all_students_page = Student.all(limit=20, offset=60).all()

new_student = Student(person_name="Ada", course="Databases")
new_student.save()  # commits and refreshes the instance
```

`Student.get(...)` builds a single-row lookup; call `.get()` for the full model instance or `.exists()` for a boolean. Multiple matches raise a multiple-results error. Pass column names to `Student.get(...)` to return only those fields, either as a scalar or a dictionary. `filter(...)` accepts exact field matches and Django-style lookups such as `course__icontains="py"`, `id__gte=10`, `id__in=[1, 2]`, and `classroom_id__isnull=True`. Supported lookups include `exact`, `iexact`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `notin`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, and `isnull`. `search(...)` applies case-insensitive substring matching across all provided string fields (all terms must match); `search_any(query, *fields)` searches one term across fields and returns rows matching any field. Use `.values()` to select all columns as dictionaries or `.values("field", ...)` for selected columns; chain `.order("field")` (prefix with `-` for descending) and then `.all()` to execute. Annotate a route argument as SQLAlchemy's `Session` when you need transaction control directly.

The sample API exposes these operations as `GET /students/filter?course__icontains=py&id__gte=1&order_by=-id&limit=20&offset=0`. Use comma-separated values for `__in`, for example `GET /students/filter?id__in=1,2,3`. Search using `GET /students/search?q=python` (searches `person_name` and `course` by default), or choose fields with `GET /students/search?q=ada&fields=person_name,course`.

Use `Count`, `Sum`, `Min`, `Max`, and `Avg` with `Model.aggregate()` or a filtered queryset. An ungrouped aggregate returns one dictionary; `group_by` returns a list of dictionaries, one per group:

```python
from lenapi import model

class Purchase(model.Model):
    user_id = model.ForeignKeyField("user.id")
    amount = model.DecimalField(max_digits=12, decimal_places=2)

total_spent = Purchase.aggregate(total=model.Sum("amount"))
# {"total": Decimal("...")}

spending_by_user = Purchase.aggregate(
    group_by="user_id",
    total=model.Sum("amount"),
    purchases=model.Count(),
)
largest_purchase = Purchase.filter(user_id=1).aggregate(maximum=model.Max("amount"))
purchase_count = Purchase.count()
```

Use `Purchase.aaggregate(...)`, `Purchase.acount()`, or `await Purchase.afilter(...).aggregate(...)` with async database access.

Delete a unique matching record with `Student.get(person_name="Ifeanyi").delete()`; it returns `True` if a row was removed. A retrieved instance also supports `student.delete()`; `.delet()` is provided as an alias. `Student.filter(course="Python").delete()` deletes matching rows and returns the count. `Student.all().delete()` drops the entire `student` table; a migration is required before querying it again.

`Student.get.all()` and `Student.filter(...).all()` return list-like collections. Collections support `.delete()` for their loaded rows, but not `.save()`; save an individual instance after changing it. Only `Student.all().delete()` drops the table.

Paginate list queries with `.all(limit=..., offset=...)`, or chain `.limit(...).offset(...).all()`. The returned list remains usable as before and now carries a `.pagination` dictionary plus `.total_count`, `.page_count`, and `.total_pages` properties. Metadata includes the total matching rows, current page, page count, limit, offset, and next/previous-page information. `Student.get.all(limit=20, offset=40)` and `Student.all(limit=20, offset=40).all()` support the same metadata. Limits and offsets must be non-negative integers.

```python
page = Student.filter(course="Python").all(limit=20, offset=40)
students = list(page)
page_count = page.page_count
total_students = page.total_count

# Include pagination metadata in an API response. Use your response schema to
# validate/serialize each model instance before returning it from a route.
return {
    "results": [StudentOutput.model_validate(row).model_dump(mode="json") for row in page],
    "pagination": page.pagination,
}
```

Update a record by assigning fields on a unique lookup result and saving it:

```python
student = Student.get(person_name="Ifeanyi")
student.course = "Java"
student.save()
```

The model also provides common get/create/update/delete shortcuts. `defaults` contains the fields to write, while keyword arguments identify the row:

```python
student = Student.get_or_404(person_name="Ifeanyi")

student = Student.create_update(
    defaults={"course": "Python"},
    person_name="Ifeanyi",
)

student = Student.update_or_404(
    defaults={"course": "Java"},
    person_name="Ifeanyi",
)

Student.delete_or_404(person_name="Ifeanyi")

student, created = Student.get_or_create(
    defaults={"course": "Python"},
    person_name="Ifeanyi",
)
```

`get_or_404`, `update_or_404`, and `delete_or_404` raise `HTTPException(404, ...)` when no record matches, which lenApi returns as an HTTP 404 response. `create_update` creates a missing row or updates an existing one and returns the model instance. `get_or_create` returns `(instance, created)`, where `created` is a boolean.

For a declared foreign-key column, `.special("field_name")` resolves its related model and exposes it under that same field name:

```python
student = Student.get(person_name="Ifeanyi").special("classroom_id")
classroom_name = student.classroom_id.name
```

Here, the field name must be a `ForeignKeyField` targeting a registered lenApi model. The value at `student.classroom_id` is the related model instance; the project example defines this as `classroom_id`.

Choose what happens to dependent records when their referenced model is deleted with `model`:

- `CASCADE`: deleting a user also deletes its verification rows.
- `SET_NULL`: keep the dependent row and set its foreign key to `NULL`; declare `nullable=True`.
- `PROTECT`: prevent deletion while dependent rows still reference the record (the default).

Use exactly one policy value per field, such as `model=cascade.delete`, `model=null`, or `model=protected`. Python's `or` operator is not a list of alternatives: `cascade.delete or null or protected` always evaluates to its first truthy value. The earlier `on_delete=` keyword remains supported as an alias.

These policies are enforced by database foreign-key actions. SQLite foreign-key enforcement is enabled automatically. `Model.delete()` reports a protected deletion as HTTP 409; apply a migration after changing an existing foreign key's policy.

`ManyToManyField` creates an association table in model metadata, so `python lenora.py migration` includes it in migrations. The default table name combines the source table and relationship field (`teacher_courses` in the example). The relationship is a set-like collection; add or remove related model instances, then save the owning model to commit:

```python
teacher = Teacher.get_or_404(id=1)
course = Course.get_or_404(id=1)
teacher.courses.add(course)
teacher.save()

teacher.courses.set([course])  # replace all related courses

courses = teacher.courses.all()
teacher.courses.remove(course)
teacher.save()
```

Pass `table_name="teacher_courses"` to `ManyToManyField` to choose the association table name. Use `back_populates="teachers"` to pair it with a reciprocal `teachers = ManyToManyField("Teacher", table_name="teacher_courses", back_populates="courses")` field on `Course`.

```python
from lenapi import Application, Database

database = Database("sqlite:///app.db")
app = Application(database=database)
```

Swap the URL to use another database:

```python
Database("mysql+pymysql://user:password@localhost/mydb")
Database("postgresql+psycopg://user:password@localhost/mydb")
```

A `Session` annotated route argument receives a session that lenApi closes after the request. Commit or roll back in the handler (or in your own service layer):

```python
from sqlalchemy.orm import Session
from lenapi import Application, Database

app = Application(database=Database("sqlite:///app.db"))

@app.get("/health")
def health(db: Session):
    return {"database_ready": db.is_active}
```

The engine is disposed on ASGI lifespan shutdown. For response serialization from SQLAlchemy objects, define a Pydantic response schema with `ConfigDict(from_attributes=True)` and set it as `response_model` on the route.

## Current shape

- ASGI HTTP request/response handling
- `@app.get`, `@app.post`, `@app.put`, `@app.patch`, `@app.delete`, and generic `@app.route`
- `{parameter}` path segments and Pydantic-validated path/query types
- async or sync route functions
- `Request` access to query parameters, raw body, and JSON body
- Pydantic request body models and response models
- mountable `Router` groups
- optional request-scoped SQLAlchemy `Session` injection
- `python lenora.py run` development command (default port `8009`; `runserver` remains an alias); `python lenora.py migration` generates and applies model migrations
- Migration commands compare the database revision with migration files and skip the database upgrade when there is no new revision to apply
- Swagger UI at `/docs/` and generated OpenAPI 3.1 at `/openapi.json`
- JSON responses by default, with `Response` and `JSONResponse` for explicit control
- Request cookie parsing through `request.cookies`, plus `Response.set_cookie()` and `Response.delete_cookie()`

The installable library lives in `lenapi/`; runnable applications and their migrations are kept separately under `app/`. The standalone wheel and source download contain the framework, packaging metadata, documentation, and library tests, but omit application code and local databases.
