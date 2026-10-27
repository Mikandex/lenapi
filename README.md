# lenApi

**LenAPI** is a modern ASGI framework shaped by real-world experience building with Python backend frameworks. It brings routing, Pydantic validation, synchronous and asynchronous database access, migrations, and interactive API documentation together in a developer-friendly toolkit.

LenAPI includes tools for common backend needs, including WebSockets, caching, background tasks, and authentication. Its social sign-in routes currently support Google, GitHub, and LinkedIn, with provider credentials and callback URLs configured by the developer.

LenAPI aims to reduce repetitive code and simplify common integrations, helping developers build APIs with less setup. Its framework source is compact, while its full-featured installation includes database and server dependencies.

**LenAPI — Compact by Design, Powerful by Capability.**

## Python versions

LenAPI is tested and supported on Python 3.11 through 3.14. Python 3.10 reached
end of life on October 1, 2026 and is unsupported; installing LenAPI requires
Python 3.11 or newer, and importing the framework on Python 3.10 reports this
requirement explicitly.

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

WebSockets keep one connection open so a client and server can send messages in
both directions without creating a new HTTP request for every update. Use them
for chat, live status, notifications, and WebRTC signaling. They are separate
from normal HTTP routes and do not appear as ordinary request/response
operations in Swagger.

### Low-level WebSocket handler

Use `@app.websocket(...)` when you need to control the protocol yourself. A
handler accepts the connection, receives and sends messages until the client
disconnects, then cleans up:

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

`WebSocket` provides `accept()`, `receive_text()`, `receive_json()`,
`receive_bytes()`, `send_text()`, `send_json()`, `send_bytes()`, and `close()`.
Catch `WebSocketDisconnect` to handle a normal client disconnect. WebSocket
handlers can also be registered on a `Router` and mounted with
`include_router()`.

### Ready-made room service

`WebSocketService` handles connection tracking and relaying for common message
types. The supported public types are `chat`, `video`, `json`, `message`, and
`byte`; `chat_video` is available for combined chat and video signaling. The
`chat` and `video` types use JSON objects, `json` accepts any JSON value,
`message` carries plain text, and `byte` carries binary data. Rooms are chosen
with `{room_id}` in the path or with a `?room=...` query parameter. JSON, text,
and byte services can also use a fixed path without a room ID.

Configure trusted browser origins in generated `settings.py`. The default
`[]` rejects WebSocket handshakes until you explicitly allow the page's exact
origin:

```python
# settings.py
WEBSOCKET_ALLOWED_ORIGINS = [
    "http://127.0.0.1:8009",  # local browser origin
    "https://app.example.com",
]
```

Origin checks protect browser connections from untrusted sites, but they do not
authenticate a user. Add `auth` when the socket is private. A service
authenticator is a sync or async callable that receives a `Request`; return an
identity to accept the connection or `None`/`False` to reject it. Browsers
cannot set arbitrary WebSocket headers, so browser clients normally authenticate
with a secure cookie or a short-lived credential your authenticator can verify.

```python
from lenapi import LENAPI, WebSocketService

app = LENAPI()

def authenticate(request):
    # Replace with your real cookie/token verification.
    session_id = request.cookies.get("session_id")
    return {"user_id": "123"} if session_id == "valid-session" else None

chat = WebSocketService(
    path="/ws/chat/{room_id}",
    type="chat",  # "video", "json", "message", or "byte"
    auth=authenticate,
    max_message_size=64_000,
    max_connections=6,  # optional per-room limit
    max_total_connections=100,  # cap across all rooms in this process
    heartbeat_seconds=30,
)
app.include_router(chat.router())
```

The relay broadcasts each incoming message to all currently connected clients in
the same room, including the sender. A chat JSON object could look like:

```json
{"type":"chat.message","text":"Hello","sender":"Ada"}
```

`heartbeat_seconds=30` sends `{"type":"lenapi.ping"}` after an idle interval;
clients should answer with `{"type":"lenapi.pong"}`. Set it to `None` to
disable application-level heartbeat checks. `max_message_size` limits incoming
and outgoing payload size in bytes. `max_connections` limits one room;
`max_total_connections` limits all connections handled by this process.

### Create room IDs and join from a browser

Create a room over HTTP, then give participants the returned path. The service
creates the in-memory room when the first participant connects; generating the
ID does not store a room record or make the ID a secret access credential.

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

Minimal browser chat client (open the page with `?room=<room_id>`):

```html
<ul id="messages"></ul>
<form id="chat-form">
  <input id="message" placeholder="Message" autocomplete="off">
  <button type="submit">Send</button>
</form>
<script>
  const roomId = new URLSearchParams(location.search).get("room");
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${location.host}/ws/chat/${roomId}`);

  socket.addEventListener("open", () => console.log("Connected to room", roomId));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "lenapi.ping") {
      socket.send(JSON.stringify({ type: "lenapi.pong" }));
      return;
    }
    const item = document.createElement("li");
    item.textContent = message.text ?? JSON.stringify(message); // Do not insert untrusted HTML.
    document.querySelector("#messages").append(item);
  });
  document.querySelector("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (socket.readyState !== WebSocket.OPEN) return;
    const input = document.querySelector("#message");
    socket.send(JSON.stringify({ type: "chat.message", text: input.value }));
    input.value = "";
  });
</script>
```

For a fixed-path JSON service, connect to `/ws/json` and send `JSON.stringify(value)`.
For `message`, call `socket.send("plain text")`. For `byte`, send an
`ArrayBuffer` or `Blob` from the browser; set
`socket.binaryType = "arraybuffer"` to receive binary frames as buffers.

`WebSocketService.send(room_id, data)` sends to clients currently connected to
that room and returns the delivered count. `disconnect(room_id)` closes all
connections in the room. These methods do not create or persist rooms; the room
must have active connections to receive a broadcast.

### Video signaling is not video transport

`type="video"` relays only JSON signaling messages with a `type` of `offer`,
`answer`, or `candidate`. Browser code still needs the WebRTC APIs
(`RTCPeerConnection`, camera/microphone capture, and ICE candidate handling).
For real networks, configure STUN/TURN; for larger multi-person calls, use an
SFU to forward media. LenAPI's room relay is not an SFU and does not carry raw
audio/video tracks. Video rooms default to two participants unless you set
`max_connections` explicitly.

### Deployment boundaries

Room membership and broadcasts are held in process memory. With multiple
workers or hosts, clients connected to different processes cannot see one
another; use a shared pub/sub layer or an external WebSocket broker for that
deployment. Connection limits are also per process. Keep room authorization
separate from origin checks, and validate permissions for each room ID before
accepting a private connection.

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
import os
from lenapi import OpenAI, Ollama

async def main():
    async with OpenAI(
        model="gpt-4.1-mini",
        api_key=os.environ["OPENAI_API_KEY"],
    ) as openai:
        answer = await openai.responses("Explain what an API is in one sentence.")
        print(answer.get("output_text", answer))

    async with Ollama(
        model="qwen3:0.6b",
        base_url="http://127.0.0.1:11434",
    ) as ollama:
        answer = await ollama.chat([
            {"role": "user", "content": "Explain what an API is in one sentence."},
        ])
        print(answer["message"]["content"])

asyncio.run(main())
```

`OpenAI` reads `OPENAI_API_KEY` from the environment unless `api_key=` is passed directly. Its `base_url` defaults to `https://api.openai.com/v1`; override it for an OpenAI-compatible endpoint. It supports the Responses API, chat completions, and embeddings. `Ollama` defaults to `http://127.0.0.1:11434`; set `base_url=` to the URL of your own Ollama server. Pull the model on that server first, for example `ollama pull qwen3:0.6b`. Do not add `/api/chat` to `base_url`; the client appends the endpoint. These calls are non-streaming and raise HTTPX errors for unsuccessful provider responses. The OpenAI Responses API is the recommended starting point for new projects; chat completions are included for compatibility. ([OpenAI API](https://platform.openai.com/docs/api-reference/responses), [Ollama chat API](https://docs.ollama.com/api/chat), [Ollama generate API](https://docs.ollama.com/api/generate))

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

For example, app-level limits are inherited by routes that do not set their own throttle. A route-specific mapping replaces the inherited mapping, so repeat any global scopes that you still want on that route:

```python
app = LENAPI(throttle={
    "ip": "300/minute",
    "anonymous": "30/minute",
    "authenticated": "600/minute",
})

# Uses all app-level limits.
@app.get("/items")
def items():
    return {"items": []}

# Replaces the app-level mapping on this route.
# Keep both limits you want enforced on login.
@app.post("/login", throttle={
    "ip": "5/minute",
    "anonymous": "30/minute",
})
def login(data: LoginInput):
    return verify_credentials(data)
```

If `/login` instead sets only `throttle={"ip": "5/minute"}`, the anonymous and authenticated global limits do not apply to that route. Use `throttle={}` to disable inherited limits deliberately.

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

### Public and private uploaded files

`MEDIA_ROOT` and `MEDIA_URL` are for files that anyone with the URL may access. For sensitive files, use `PRIVATE_MEDIA_ROOT` (generated as `BASE_DIR / "private_media"`) and save with `private=True`. Private files have no built-in public URL. Serve them only inside an authenticated route, after checking that the authenticated user may access that specific record:

```python
from lenapi import Depends, File, HTTPException, UploadFile
from app.models import Student

@app.post("/students/{student_id}/private-image", auth=app.authenticator)
async def upload_private_image(
    student_id: int,
    image: UploadFile = File(...),
    identity=Depends(app.authenticator),
):
    student = Student.get_or_404(id=student_id)
    if not can_edit_student(identity, student):  # implement for your identity and ownership model
        raise HTTPException(404, "Not found")

    filename = await image.save("profiles", private=True)
    student.private_profile_image = f"profiles/{filename}"
    student.save()
    return {"saved": True}

@app.get("/students/{student_id}/private-image", auth=app.authenticator)
def get_private_image(student_id: int, identity=Depends(app.authenticator)):
    student = Student.get_or_404(id=student_id)
    if not can_view_student(identity, student):
        raise HTTPException(404, "Not found")
    if not student.private_profile_image:
        raise HTTPException(404, "Not found")
    return app.private_file(student.private_profile_image, inline=True)
```

Add a separate nullable `private_profile_image = model.ImageField(upload_to="profiles", nullable=True)` field to `Student` and run `python lenora.py migration`. `app.private_file()` confines paths to `PRIVATE_MEDIA_ROOT`, disables caching, and only serves supported raster images inline; other files download as attachments. It does not perform authentication or ownership checks. Load its relative path from the authorized model record, never directly from request input. Image signature validation is not malware scanning.

Install LenAPI from PyPI, or install an editable copy while working on the framework. Then create an application scaffold and run it separately:

```bash
python -m pip install lenapi
# For framework development from the repository root, use instead:
python -m pip install -e .
lenora startproject my_api
cd my_api
python lenora.py migration
python lenora.py run
```

Use the installed `lenora` command to create the first project; `python lenora.py ...` commands are available after entering the generated project. The server listens on `127.0.0.1:8009` by default. The generated application contains `app/models.py`, `app/schemas.py`, `settings.py`, `main.py`, a `tests/` package, a project-local CLI launcher, `alembic.ini`, and a `migrations/` folder. The default `User` model and authentication implementation live in the framework. The initial migration includes the framework's `User` table automatically. `LENAPI()` sets up `app.authenticator` from `settings.AUTHENTICATION`, but route protection is opt-in: pass an authenticator to `LENAPI(auth=...)`, `Router(auth=...)`, or a route's `auth=` parameter. This keeps login and registration routes public unless you protect them too. The framework repository contains the installable `lenapi` package; create an application scaffold separately with `lenora startproject`.

If `settings.py` is accidentally deleted, run `python lenora.py repair-settings` from the project directory. It recreates the generated defaults only when the file is missing and refuses to overwrite an existing settings file. The recreated file gets fresh `SECRET_KEY` and `API_KEY` values; review it before starting the app. The old `SECRET_KEY` cannot be recovered, so tokens signed with that key will no longer validate.

Run `python lenora.py apikey` inside a project to set `AUTHENTICATION = "api_key"`, add the `APIKey` model to `app/models.py` when it is missing, and run the migration workflow. The command is safe to repeat; it leaves an existing model in place and applies no migration when the database is already current. **The built-in API-key authenticator uses the framework `User` model.** It hashes the presented key and compares it with the digest in `User.api_key`; `register_user(..., authentication="api_key")` writes that digest and returns the raw key once. The generated `APIKey` table is a separate, optional app-managed key registry, linked to `User` by `user_id`; the default authenticator and `register_user()` do not automatically read or populate it. If your app wants that table to be the authentication lookup source, implement and configure that lookup explicitly. Never store raw keys in `key_hash`.

`settings.py` centralizes the API title/version, docs and OpenAPI URLs, host, port, database selection, token settings, and shared security storage. `HOST = ""` uses `127.0.0.1`; set `HOST` directly to bind another address. `app = LENAPI()` loads project settings automatically. Each generated project gets a unique high-entropy `SECRET_KEY`; `TOKEN` uses it to sign tokens and configures token lifetime and issuer. Keep the key private. Changing it invalidates tokens signed with the old key. `DATABASE_TYPE` chooses one backend (`sqlite`, `mysql`, `postgresql`, or `mongodb`), and `DATABASE_URL` configures it. `ACTIVATE_ASYNC` enables asynchronous access using the same database URL. The default database is SQLite.

Generated projects set `DEBUG = True` for local development. Unexpected route or dependency errors then print a traceback to the terminal. LenAPI returns a generic `500 Internal Server Error` response to the client in both debug modes; request validation errors and explicit `HTTPException` responses keep their normal status codes. Set `DEBUG = False` in production to stop LenAPI from printing exception tracebacks. If `DEBUG` is absent, LenAPI defaults to `False`.

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
from lenapi import authentication
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
    paths=(app.docs_url, app.redoc_url, app.openapi_url),
    session_factory=app.session_factory,
    async_session_factory=app.async_session_factory,
)
```

`GZipMiddleware` uses Python's standard library, compresses eligible response types when the request accepts gzip, and streams responses after buffering up to `minimum_size` (500 bytes by default). It adds `Content-Encoding: gzip` and `Vary: Accept-Encoding`, skips already-encoded and partial responses, and supports `compresslevel` from 0 to 9. It does not require Starlette. Custom middleware follows the ASGI callable contract: `Middleware(app, **options)` and `await app(scope, receive, send)`. Request IDs and elapsed time are returned in `X-Request-ID` and `X-Process-Time-Ms`.

The middleware in this example is optional; add only the behavior your app needs:

- `CORSMiddleware` controls which browser origins may read responses. List explicit origins for production. For cookie credentials, set `allow_credentials=True` and list exact origins; wildcard origin cannot be combined with credentials. `allow_methods` and `allow_headers` must include the methods and request headers your frontend uses.
- `GZipMiddleware` compresses eligible response content when the client sends `Accept-Encoding: gzip`. `minimum_size` controls the response size threshold, while `compresslevel` trades CPU time for compression ratio. Already-compressed, partial, `HEAD`, and no-transform responses are passed through.
- `RequestIDMiddleware` accepts a safe incoming `X-Request-ID` or creates a random ID, stores it in ASGI request state, and returns it in the response header. Use it to correlate a request with logs.
- `TimingMiddleware` returns elapsed application time in `X-Process-Time-Ms`. This is the time measured by the app middleware, not a full end-to-end network latency measurement.
- `DocsAuthMiddleware` protects the selected Swagger, ReDoc, and OpenAPI paths with an authentication dependency. The built-in `SAFE_GUARD_DOCS_URL` option is an alternative for database-backed admin login.

For example, allow a browser frontend to send bearer tokens and a partner HMAC header, expose the request ID to browser JavaScript, and enable gzip:

```python
app.add_middleware(RequestIDMiddleware)
app.add_middleware(TimingMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://frontend.example"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["authorization", "content-type", "X-Flutterwave"],
    expose_headers=["X-Request-ID", "X-Process-Time-Ms"],
)
```

When adding `DocsAuthMiddleware` directly, include every configured documentation URL in its `paths`, including `app.redoc_url` if ReDoc is enabled. Middleware order affects which component sees the request and response first.

## Database transactions

Use a database transaction context when multiple writes must commit or roll back together. Use the yielded SQLAlchemy session for every operation in that block:

```python
with app.database.transaction() as db:
    db.add(first_record)
    db.add(second_record)
```

For async work, use `async with app.async_database.transaction() as db:` and await each SQLAlchemy operation. The context commits on success, rolls back if an exception escapes, and closes the session.

## Cache service

`app.cache` is an async JSON-value cache. Set `CACHE_BACKEND` in `settings.py` to `"memory"` (default), `"database"`, or `"redis"`. SQL database-backed cache uses the shared `lenapi_security_store` table, so run `python lenora.py migration` first. To use Redis, set `CACHE_BACKEND = "redis"` and configure `REDIS_URL`:

```python
# settings.py
REDIS_URL = "redis://127.0.0.1:6379/0"
CACHE_BACKEND = "redis"
```

The URL format is `redis://<host>:<port>/<database-number>`; this example connects to Redis on the same machine, port 6379, database 0. Install the Python Redis client extra into the project's active environment with `python -m pip install 'lenapi[redis]'`. The extra installs the Python client library, not the Redis server. Install and start Redis separately, then check connectivity with `redis-cli ping` (it should return `PONG`). In zsh, keep the square brackets quoted as shown so the shell does not interpret them as a filename pattern.

`CACHE_BACKEND = "redis"` selects Redis for `app.cache` only. To use Redis as the shared store for sessions, opaque bearer tokens, IP cooldowns, request throttles, and HMAC nonce replay checks, also set `SECURITY_STORAGE = "redis"`. Both settings use `REDIS_URL`. You can choose one Redis-backed feature without moving the others; for example, keep `SECURITY_STORAGE = "database"` while setting `CACHE_BACKEND = "redis"`.

```python
await app.cache.set("profile:42", {"name": "Ada"}, ttl=300)
profile = await app.cache.get("profile:42")
await app.cache.delete("profile:42")
```

## Webhooks

A webhook is an HTTP request one service sends to another when an event occurs.
Start with a plain endpoint that accepts an event body; add sender verification
when the integration requires it. LenAPI offers `webhook_auth()` for providers
that sign the raw body and `hmac_auth()` for partners that can use LenAPI's
canonical request-signing format. These are different formats; choose the
helper that matches the sender.

### Plain webhook without verification

This minimal route accepts and returns arbitrary JSON. It has no signature or
authentication check, so use it for local testing, trusted internal traffic,
or nonsensitive events. HTTPS protects the connection but does not verify who
sent the event.

```python
from typing import Any
from lenapi import Body, LENAPI

app = LENAPI()

@app.post("/webhooks/events")
async def receive_event(payload: Any = Body()):
    # Inspect or validate the event before processing it.
    return {"received": True, "event": payload}
```

Before exposing this route publicly or triggering sensitive actions, add one
of the verification options below. Do not rely on a hard-to-guess URL as
authentication.

### Provider raw-body HMAC

`webhook_auth()` verifies a signature over the exact request body. Configure
header names to match the provider. If the provider sends stable event IDs,
passing `app.security_store` enables deduplication across requests:

```python
from typing import Any
from lenapi import Body, Depends
from lenapi.security import webhook_auth
import settings

provider_hook = webhook_auth(
    settings.WEBHOOK_SECRET,  # at least 32 bytes
    security_store=app.security_store,
    provider="billing",
    signature_header="x-webhook-signature",
    event_id_header="x-webhook-id",
    timestamp_header="x-webhook-timestamp",  # omit if provider has none
    tolerance_seconds=300,
)

@app.post("/webhooks/billing", auth=provider_hook)
async def receive_event(
    payload: Any = Body(),
    verified=Depends(provider_hook),
):
    # Validate event type and fields before changing application data.
    return {"received": True, "event_id": verified["event_id"]}
```

When `timestamp_header` is configured, LenAPI verifies freshness and signs the
timestamp together with the raw body. With `security_store` and an event ID
header, duplicate event IDs are acknowledged with HTTP 200 without running the
handler again. Database-backed security storage requires
`python lenora.py migration`; Redis can be used for shared multi-worker state.

### Partner HMAC requests

Use `hmac_auth()` when both services can share a secret and the sender can sign
LenAPI's canonical message: timestamp, method, path and query, SHA-256 digest
of the exact body, and a unique nonce. It checks timestamp freshness and nonce
reuse. The sender must sign and transmit the same body bytes and agree on the
signature header. See the [HMAC section](#hmac-request-authentication) below for
complete server and client examples.

### Providers without HMAC

If a provider cannot sign requests but supports a bearer token or secret header,
require that credential and compare it in constant time. This example uses a
dedicated header; it does not authenticate or sign the payload itself:

```python
import hmac
from typing import Any
from lenapi import Body, Depends, HTTPException, Request
import settings

async def verify_provider_key(request: Request):
    supplied = request.headers.get("x-provider-key", "")
    expected = settings.PROVIDER_WEBHOOK_KEY
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Invalid webhook credential")
    return {"provider": "billing"}

@app.post("/webhooks/provider", auth=verify_provider_key)
async def receive_provider_event(
    payload: Any = Body(),
    identity=Depends(verify_provider_key),
):
    # Validate the payload and deduplicate its provider event ID.
    return {"received": True, "provider": identity["provider"]}
```

Use HTTPS, keep the key out of source control, restrict the endpoint at a
trusted network boundary when possible, and rotate the key if exposed. A static
credential does not prove payload integrity or freshness and does not prevent a
captured request from being replayed. Do not leave a state-changing webhook
public merely because its URL is hard to guess. If a sender supports no
signature or credential, only accept events through a trusted gateway or keep
the endpoint non-sensitive and read-only.

## Durable background jobs

`BackgroundTasks` remains useful for short in-process work after a response. For a worker queue, `BackgroundQueue` supports `database`, `redis`, or `cache`/`memory` backends. The database queue table is excluded from ordinary migrations. Export a `queue` or `background_queue` from `main.py`, then run `python lenora.py background-worker`; it creates and applies the queue migration before starting the worker. Redis mode requires `python -m pip install 'lenapi[redis]'` and Redis persistence configured for the durability you need. The cache/memory backend is process-local and ephemeral: queued jobs are lost when the process exits and cannot be shared with a separate worker process. Jobs on database and Redis backends are delivered at least once; write task handlers to be idempotent.

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

Swagger UI is available at [http://127.0.0.1:8009/docs/](http://127.0.0.1:8009/docs/), ReDoc at [http://127.0.0.1:8009/redoc/](http://127.0.0.1:8009/redoc/), and the generated OpenAPI document at [http://127.0.0.1:8009/openapi.json](http://127.0.0.1:8009/openapi.json). All three are enabled by default and use the same OpenAPI schema. Set `API_TITLE`, `API_VERSION`, `DOCS_URL`, `REDOC_URL`, and `OPENAPI_URL` in `settings.py` to configure the documentation metadata and paths; set `REDOC_URL = None` to disable ReDoc. You can also pass overrides directly to `LENAPI(title="My API", version="1.0.0", docs_url="/docs/", redoc_url="/redoc/", openapi_url="/openapi.json")`.

Protect both the Swagger page and its OpenAPI JSON with `DocsAuthMiddleware`. It accepts the same callable authentication dependencies used by routes; pass the configured docs paths so customized URLs are protected too:

```python
from lenapi import LENAPI, DocsAuthMiddleware
from lenapi import authentication

app = LENAPI()
docs_auth = authentication("basic_login")
app.add_middleware(
    DocsAuthMiddleware,
    auth=docs_auth,
    paths=(app.docs_url, app.redoc_url, app.openapi_url),
    session_factory=app.session_factory,
    async_session_factory=app.async_session_factory,
)
```

The session factories let database-backed authentication such as Basic or API-key auth query the configured user model. This middleware only protects the documentation endpoints; it does not secure API routes. Omit it to keep the default public docs, or pass another authentication dependency such as JWT or API-key auth.

For the built-in docs login, set `SAFE_GUARD_DOCS_URL = True` in `settings.py`. The app then serves an administrator sign-in page at `/` and guards `/docs/`, `/redoc/`, and `/openapi.json`; successful login redirects to Swagger. Failed sign-ins are limited to 5 per client IP in a 300-second window by default; configure `DOCS_LOGIN_MAX_ATTEMPTS` and `DOCS_LOGIN_WINDOW_SECONDS` to adjust this. With `SECURITY_STORAGE = "database"` or `"redis"`, the limiter uses shared storage and coordinates across workers; `"memory"` keeps counters per process and is intended for single-process development. Create an administrator with `python lenora.py admin` after `python lenora.py migration`; the password prompt is hidden and the framework stores only a password hash. The framework's `AdminUser` and expiring session storage are created by the first migration. Admin records and sessions use the selected application database, including MongoDB; Redis is used only for attempt counters when selected. The session cookie is HttpOnly and expires after `DOCS_SESSION_LIFETIME_SECONDS` (default 3600 seconds). `DOCS_COOKIE_SECURE` follows the ASGI scheme by default; set it to `True` when TLS terminates at a reverse proxy.

The docs gate only handles `/`, `/logout`, the configured Swagger and ReDoc paths, and the configured OpenAPI path. Regular application routes do not query the admin or session tables; they pass through a small path check. Opening protected docs or OpenAPI does validate the session against the configured database, which is the expected cost of revocable server-side sessions.

To remove the gate, set `SAFE_GUARD_DOCS_URL = False` and restart the app. The admin tables may remain in the database; they are not queried while the feature is disabled.

### Built-in model admin and resource monitor

The optional admin console is separate from the Swagger/ReDoc login and is served at `/admin/`. It uses stateless HTTP Basic authentication for its API requests. Enable it in `settings.py`, then run a migration and create an administrator. The first CLI-created superadmin has full access and can create other admin accounts and grant per-model permissions. If the project already had docs-admin accounts before this feature, create an additional account with the CLI; it becomes superadmin if no superadmin exists yet.

```python
# settings.py
ADMIN_ENABLED = True
ADMIN_URL = "/admin/"
ADMIN_SESSION_LIFETIME_SECONDS = 3600
ADMIN_COOKIE_SECURE = None  # follows the ASGI HTTPS scheme

# Monitoring is optional and requires the admin console to be enabled.
ADMIN_MONITORING_ENABLED = True
```

```bash
python lenora.py migration
python lenora.py admin
python lenora.py run
```

The first SQL migration includes the framework `AdminUser` fields and session storage along with the imported app models. MongoDB initializes the admin collections through `migration`. Mongo collections need a corresponding `MongoDocument` class so the admin can safely render typed edit forms.

Create `app/admin.py` to explicitly allow-list models and choose which fields are visible. Once this file exists, only models registered there appear in the console. `fields` selects displayed/editable fields, `readonly` displays selected fields without allowing changes, and `exclude` hides fields. Credential-like fields such as `password`, `api_key`, and `key_digest` remain hidden even if listed; use application routes that hash or rotate credentials.

```python
# app/admin.py
from lenapi.admin import admin
from app.models import Student, Course, Invoice, Staff

admin.register(
    Student,
    fields=("person_name", "course", "email", "profile_image"),
    readonly=("email",),
    exclude=("password", "private_notes"),
)

admin.register(
    Course,
    fields=("title", "description", "duration_weeks", "is_active"),
)

admin.register(
    Invoice,
    fields=("invoice_number", "student_id", "amount", "status", "issued_at"),
    readonly=("invoice_number", "issued_at"),
    exclude=("payment_token", "provider_response"),
)

admin.register(
    Staff,
    fields=("name", "work_email", "department"),
    readonly=("work_email",),
    exclude=("password", "salary", "hr_notes"),
)
```

When `app/admin.py` is absent, the console keeps its backward-compatible model discovery. Registered models show only the selected columns; readonly fields appear in record forms without editable controls, and excluded fields are omitted from both the UI and the API response. Update requests are checked against the same field allow-list. The console supports list/read, update, and delete; record creation is handled by application endpoints. Superadmins grant `view`, `read`, `write`, and `delete` independently. `view` controls whether a model appears in the sidebar, `read` allows listing records, `write` permits updates, and `delete` controls deletion.

Admin accounts and model records use the selected SQL database or MongoDB. Disable the console with `ADMIN_ENABLED = False`; set `ADMIN_MONITORING_ENABLED = False` to hide its monitor. Resource metrics are available only to a superadmin when both settings are enabled:

```bash
python -m pip install 'lenapi[monitoring]'
```

The Resource Monitor page shows CPU and RAM metric cards with rolling graphs, plus the LenAPI process PID and thread count. RAM shows process resident memory and total system-memory use. Metrics refresh every two seconds while the page is open; history remains in that browser page only. The endpoint is restricted to superadmins. This is an operational status view, not a host monitoring or alerting system. Monitoring requires psutil and does not start background sampling.

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
from lenapi import Depends, LENAPI

app = LENAPI()

def current_user(user_id: int):
    return {"id": user_id, "name": "Tolu"}

@app.post("/profile/{user_id}")
def profile(user: dict = Depends(current_user)):
    return user
```

When a request calls `POST /profile/12`, LenAPI resolves the matching `user_id` path parameter and passes it into `current_user`. The dependency's returned dictionary is injected as `user` into `profile`.

Dependencies are cached once per request, so if multiple parameters use the same dependency callable, it runs once. Synchronous routes, dependencies, authentication and token-lookup callbacks, HMAC nonce-store methods, and `DocsAuthMiddleware` authenticators run in a worker thread so blocking I/O in them does not block the ASGI event loop. Async callbacks run on the event loop; use async I/O there or explicitly offload blocking work yourself.

## Authentication: global, router, and route scope

LenAPI authentication has two steps: choose/configure an authenticator, then attach it where it should run. `settings.AUTHENTICATION` selects the project's `app.authenticator` using the framework `User` model. It does not protect every route automatically and does not create login, registration, token, refresh, or profile endpoints. Add protection with `LENAPI(auth=...)` for all app routes, `Router(auth=...)` for a route group, or a route decorator's `auth=` for one endpoint. The route authenticator is enforced before the handler and appears in Swagger's security metadata.

App-wide auth is appropriate when every HTTP route should require the same credential. Create the authenticator and pass it to `LENAPI`:

```python
from lenapi import Depends, LENAPI, authentication

# Reads settings.AUTHENTICATION, TOKEN, and the framework User model.
auth = authentication("jwt")
app = LENAPI(auth=auth)

@app.get("/account")
def account(identity=Depends(auth)):
    return {"identity": identity}
```

Because app-wide auth protects every application route, prefer router or endpoint scope when the same app also needs public registration or sign-in routes. Router scope is commonly used with the settings-configured authenticator:

```python
from lenapi import Depends, LENAPI, Router

app = LENAPI()
auth = app.authenticator
api = Router(prefix="/api", auth=auth)

@api.get("/account")
def account(identity=Depends(auth)):
    return {"identity": identity}

app.include_router(api)
```

For one protected endpoint, put `auth=auth` directly on its decorator. `Depends(auth)` gives the handler the verified identity; using only `auth=` still enforces the check without injecting a value. Route-level `auth=` takes precedence over the router default. The setting `AUTHENTICATION` accepts `jwt`, `api_key`, `basic_login`, `opaque_bearer`, or `session`; HMAC and OIDC can be added as callable dependencies too.

When using a non-default identifier such as email or phone, that column must exist on the configured user model and your registration or provisioning flow must save its value. The generated `UserRegistration` schema asks for username by default; extend it or provide a custom registration route when collecting another identifier.

## JWT bearer authentication

Set the signing settings in `settings.py`. A generated project starts with a unique `SECRET_KEY` and uses it in `TOKEN`:

```python
AUTHENTICATION = "jwt"
TOKEN = {
    "SECRET_KEY": SECRET_KEY,
    "LIFETIME_SECONDS": 3600,
    "REFRESH_LIFETIME_SECONDS": 2592000,
    "ISSUER": API_TITLE,
}
```

`LENAPI()` creates `app.authenticator` from these settings and the framework `User` model. Mount its token router on an app that does not have app-wide auth, then protect private routes with that same authenticator:

```python
from lenapi import Depends, LENAPI, Router
from lenapi.authentication import UserRegistration, register_user

app = LENAPI()
jwt = app.authenticator
app.include_router(jwt.router())  # POST /auth/token and POST /auth/refresh

public = Router(prefix="/auth")
@public.post("/register")
def register(data: UserRegistration):
    return register_user(data=data, authentication="jwt")

api = Router(prefix="/api", auth=jwt)

@api.get("/profile")
def profile(identity=Depends(jwt)):
    return {"user_id": identity["sub"]}

app.include_router(api)
app.include_router(public)
```

The framework-backed JWT authenticator checks the submitted username and password against the database `User` model and its stored password hash. `POST /auth/token` accepts `{"username": "...", "password": "..."}` and returns an access token plus a refresh token. Send the access token as `Authorization: Bearer <access_token>`. `POST /auth/refresh` accepts `{"refresh_token": "..."}` and returns replacement tokens. The token and refresh routes are intentionally hidden from Swagger; protected API routes still show their Bearer scheme and Swagger's **Authorize** control. These routes have a default IP limit of 10 requests per minute, including failed credential checks; pass `throttle={"ip": "5/minute"}` to `jwt.router(...)` for a different limit. In production, use shared `SECURITY_STORAGE = "database"` or `"redis"` for refresh replay protection across workers. Refresh tokens are single-use; the in-memory fallback coordinates only within one process.

For a custom user table, create the JWT dependency with `authentication("jwt", user_model=YourUser)`. Basic and JWT can use your model as long as it has the selected identifier column and a `password` column containing LenAPI-compatible password hashes. For Basic, select the identifier with `BASIC_AUTH_FIELD`. Or configure `JWTAuth` with an `authenticate(username, password)` callback. That callback must verify a password hash and return the user's string ID, `(string_id, claims)`, or `None`. `JWTAuth` signs and verifies HS256 tokens, checks expiry and configured issuer, and rejects refresh tokens when used as access tokens. Keep the signing key secret and serve credentials only over HTTPS.

## API-key authentication

The built-in user API-key flow uses random URL-safe keys. The raw key is returned once at registration; LenAPI stores only its SHA-256 digest in `User.api_key`. To enable the setting and generate the app's additional key-management model, run this from the generated project directory:

```python
# settings.py
AUTHENTICATION = "api_key"
```

```bash
python lenora.py apikey
```

The CLI sets that setting, adds `APIKey` to `app/models.py` if it is absent, and runs the migration workflow. The generated model has `user_id`, `key_hash`, `is_active`, `created_at`, and `revoked_at`. The default authenticator uses the framework `User` model and checks the hash in `User.api_key`. The CLI-created `APIKey` table is a separate management table linked to a `User`; it is not the default lookup table, and `register_user()` does not add rows to it automatically.

For quick development, use the framework `User` model and its `api_key` column with no custom arguments. To use your own model, provide both `user_model` and `api_key_field`; there is no implicit key-column fallback for this custom-model path. LenAPI validates the field during setup and raises a clear error if it is missing:

```python
from lenapi import authentication, hash_api_key, make_api_key, model

class Customer(model.Model):
    username = model.StringField(max_length=120, unique=True)
    password = model.StringField(max_length=255)
    key_digest = model.StringField(max_length=64, unique=True, nullable=True)

auth = authentication(
    "api_key",
    user_model=Customer,
    api_key_field="key_digest",
)

# In a trusted provisioning flow, show the raw key once and store only its digest.
raw_key = make_api_key()
customer = Customer.get(id=1).get()
customer.key_digest = hash_api_key(raw_key)
customer.save()
```

When using `register_user()` with a custom model, also pass `api_key_field="key_digest"`. The model and registration schema must include the fields your registration flow writes.

Create users through your registration endpoint with the framework helper. Its response includes the one-time raw key; the database row stores only the digest:

```python
from lenapi import LENAPI, Router
from lenapi.authentication import UserRegistration, register_user

app = LENAPI()
api = Router(prefix="/auth")  # Keep registration public.

@api.post("/register")
def register(data: UserRegistration):
    return register_user(data=data, authentication="api_key")

app.include_router(api)
```

After registration, copy the `api_key` from that response and keep it securely; it cannot be recovered from the database. Send it on later requests as `X-API-Key`:

```python
from lenapi import Depends, LENAPI, Router

app = LENAPI()
auth = app.authenticator  # settings.AUTHENTICATION = "api_key"
api = Router(prefix="/api", auth=auth)

@api.get("/profile")
def profile(identity=Depends(auth)):
    return {"user_id": identity["user_id"]}

app.include_router(api)
```

For an existing user, issue or rotate a key in a trusted provisioning flow: generate it with `make_api_key()`, store `hash_api_key(raw_key)` in `User.api_key`, and show the raw value to the user once. Never log it or return it from ordinary profile endpoints. The generated `APIKey` table is not currently the lookup table used by `app.authenticator`; applications that use it directly must implement the corresponding lookup and revocation policy.

## Database-backed HTTP Basic authentication

Basic authentication sends a username-like identifier and password on every request using the HTTP `Authorization: Basic ...` header. LenAPI checks the submitted credentials against the configured user model and verifies the stored password hash. Choose the identifier field in `settings.py`:

```python
AUTHENTICATION = "basic_login"
BASIC_AUTH_FIELD = "username"  # Can be email, phone, or another User column.
```

Use `app.authenticator` on a router or route. Registration remains a public route unless you protect it separately; use `register_user()` to hash passwords consistently:

```python
from lenapi import Depends, LENAPI, Router
from lenapi.authentication import UserRegistration, register_user

app = LENAPI()
basic_auth = app.authenticator
public = Router(prefix="/auth")
private = Router(prefix="/api", auth=basic_auth)

@public.post("/register")
def register(data: UserRegistration):
    return register_user(data=data, authentication="basic_login")

@private.get("/profile")
def profile(identity=Depends(basic_auth)):
    return {"user_id": identity}

app.include_router(public)
app.include_router(private)
```

Call the protected endpoint with Basic credentials; for example, `requests.get(url, auth=("ada", "password"))`. If `BASIC_AUTH_FIELD = "email"`, send the email in the username position: `auth=("ada@example.com", "password")`. Use HTTPS because Basic credentials are encoded, not encrypted, by the header scheme itself.

### Server-side session authentication

`session_auth()` reads the configured cookie, then loads the associated identity from the security store. Create a random session ID only after validating the user's credentials. Store session state server-side and send just the ID in an HttpOnly cookie:

```python
import json
import secrets
from lenapi import Depends, JSONResponse, LENAPI
from lenapi import Request
from lenapi.security import session_auth

app = LENAPI()  # SECURITY_STORAGE defaults to "database".
session = session_auth(store=app.security_store, cookie_name="session_id")

@app.post("/session/login")
async def login():
    # Replace with real credential verification before issuing a session.
    identity = {"user_id": "123"}
    session_id = secrets.token_urlsafe(32)
    await app.security_store.set(
        f"session:{session_id}", json.dumps(identity), ttl=3600
    )
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        "session_id", session_id, max_age=3600, path="/",
        httponly=True, secure=True, samesite="lax",
    )
    return response

@app.get("/session/me")
def session_me(identity=Depends(session)):
    print(identity)
    return identity

@app.post("/session/logout")
async def logout(request: Request, identity=Depends(session)):
    session_id = request.cookies.get("session_id")
    await app.security_store.delete(f"session:{session_id}")

    response = JSONResponse({"message": "Logged out"})
    response.delete_cookie("session_id")
    return response
```

Use `secure=True` in production behind HTTPS; use `secure=False` only for local plain-HTTP development. The logout route requires a valid session, deletes its server-side record, then expires the browser cookie. Cookie-authenticated state-changing routes also need CSRF protection.

### Opaque bearer authentication

Opaque bearer tokens are random values looked up on every request. Unlike JWTs, they contain no trusted claims themselves; deleting the stored entry revokes one immediately.

```python
import json
import secrets
from lenapi import Depends, JSONResponse, LENAPI, Request
from lenapi.security import opaque_bearer_auth

app = LENAPI()
bearer = opaque_bearer_auth(store=app.security_store)

async def issue_token(identity: dict) -> str:
    token = secrets.token_urlsafe(32)
    await app.security_store.set(
        f"opaque_bearer:{token}", json.dumps(identity), ttl=3600
    )
    return token

@app.post("/opaque/token")
async def token(request: Request):
    # Verify the user's credentials before issuing a token in a real app.
    value = await issue_token({"user_id": 1, "name": "Tolu"})
    return JSONResponse({"token": value})

@app.get("/opaque/profile", auth=bearer)
def profile(request: Request, identity=Depends(bearer)):
    # Do not log the raw bearer token in production.
    return identity

async def revoke_token(token: str) -> bool:
    await app.security_store.delete(f"opaque_bearer:{token}")
    return True

@app.post("/opaque/revoke", auth=bearer)
async def revoke(request: Request, identity=Depends(bearer)):
    token = request.headers["authorization"].removeprefix("Bearer").strip()
    await revoke_token(token)
    return {"message": "Token revoked"}
```

The token endpoint above uses a sample identity to demonstrate the storage flow; replace it with credential verification. The `.strip()` after removing `Bearer` is needed so revocation looks up the same token value that was stored.

### HMAC request authentication

HMAC is useful when another service sends webhooks to your API, or when your service signs requests sent to another HMAC-enabled API. Both parties share a secret. The sender signs the timestamp, HTTP method, path and query, exact request-body bytes, and a unique nonce; the receiver recomputes the signature and compares it before calling the route. HMAC does not issue bearer tokens or replace user login. The shared secret must contain at least 32 bytes.

```python
# Add a securely generated, stable secret to your settings.py, or load it
# from your preferred secret manager. Share it with the partner securely.
PARTNER_HMAC_SECRET = "<same random secret configured for the partner>"

from typing import Any
from lenapi import Depends, LENAPI, Request
from lenapi.security import hmac_auth

app = LENAPI()  # SECURITY_STORAGE defaults to the database.
partner_auth = hmac_auth(
    PARTNER_HMAC_SECRET,
    tolerance_seconds=300,
    nonce_store=app.security_store,
    header_name="X-Flutterwave",
)

@app.post("/partner/webhook", auth=partner_auth)
async def receive_partner_webhook(
    request: Request,
    identity=Depends(partner_auth),
):
    body: Any = await request.json()
    # Validate event structure and business rules before processing it.
    return {"accepted": True, "body": body}
```

`header_name` names the signature header only. The timestamp and nonce headers remain `X-LenAPI-Timestamp` and `X-LenAPI-Nonce`. Passing `app.security_store` as `nonce_store` makes replay checks shared across workers; without it, replay tracking is process-local. HMAC verification signs the raw body bytes before the handler parses arbitrary JSON with `request.json()`.

The partner does not need to install LenAPI. This standalone Python client uses the standard library to sign a compact JSON body and sends those exact bytes with `requests`:

```python
import hashlib
import hmac
import json
import secrets
import time
import requests

client_secret = "same-shared-secret-configured-on-the-server"
url = "https://api.example.com/partner/webhook"
method = "POST"
path = "/partner/webhook"
payload = {"event": "payment.completed", "reference": "order-123"}

# Serialize once, sign these bytes, and send the same bytes unchanged.
body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
timestamp = str(int(time.time()))
nonce = secrets.token_urlsafe(16)
body_digest = hashlib.sha256(body).hexdigest()
message = f"{timestamp}\n{method}\n{path}\n{body_digest}\n{nonce}".encode("utf-8")
signature = hmac.new(client_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

headers = {
    "Content-Type": "application/json",
    "X-LenAPI-Timestamp": timestamp,
    "X-LenAPI-Nonce": nonce,
    "X-Flutterwave": signature,
}
response = requests.post(url, data=body, headers=headers, timeout=15)
print(response.status_code, response.json())
```

The signature is HMAC-SHA256 over `timestamp + newline + uppercase method + newline + path-and-query + newline + lowercase-hex SHA-256 body digest + newline + nonce`. The signature header must match the server's `header_name`; path and body must exactly match what the server receives. Use HTTPS and never place a production secret in source code or logs.

#### Sign an outgoing request from a LenAPI application

HMAC uses a shared secret rather than issuing a separate token per request. Generate and provision the secret to the partner through a secure channel. A LenAPI client can use `hmac_request_signature()` for an outbound request; the receiving service must verify the same message format and header names.

```python
import json
import secrets
import time
import requests
from lenapi.security import hmac_request_signature

shared_secret = "same-secret-provisioned-to-the-receiver"
url = "https://partner.example.com/events"
method = "POST"
path = "/events"
payload = {"event": "account.updated", "account_id": "acct-42"}
body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
timestamp = str(int(time.time()))
nonce = secrets.token_urlsafe(16)
signature = hmac_request_signature(
    shared_secret, method, path, timestamp, body, nonce=nonce
)

response = requests.post(
    url,
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-LenAPI-Timestamp": timestamp,
        "X-LenAPI-Nonce": nonce,
        "X-LenAPI-Signature": signature,
    },
    timeout=15,
)
print(response.status_code, response.text)
```

### OIDC bearer-token validation

LenAPI has two distinct ways to use provider identity:

- Use **`SocialAuth`** when you want LenAPI to provide browser sign-in and callback routes for a supported provider. After sign-in it can find or create a local user and return a LenAPI JWT for your API.
- Use **`oidc_auth()`** when the client already obtains an OIDC token through a separate login flow and sends it as `Authorization: Bearer <token>`. Your verification callback validates the token and returns identity claims; LenAPI uses those claims to protect routes.

Choose `oidc_auth()` when your frontend/mobile client owns the OIDC flow, your API trusts an external provider directly, or you need a provider flow not handled by built-in `SocialAuth`. It does not create login/callback endpoints, register users, or issue a new token.

#### Protect a route with an externally issued OIDC token

The verifier must use a maintained OIDC/JWT library to validate the token cryptographically: verify its signature against trusted provider keys and check issuer, audience, expiry, and any required nonce. Merely decoding a JWT is not validation.

```python
from lenapi import Depends, LENAPI
from lenapi.security import oidc_auth

app = LENAPI()

def verify_provider_token(token: str):
    # Replace this adapter with your OIDC library's verified validation call.
    claims = verify_with_provider_keys(
        token,
        issuer="https://issuer.example",
        audience="your-client-id",
    )
    if claims is None:
        return None
    return {
        "sub": claims["sub"],
        "email": claims.get("email"),
        "scope": claims.get("scope", ""),
    }

provider_auth = oidc_auth(verify_provider_token)

@app.get("/oidc/profile", auth=provider_auth)
def oidc_profile(identity=Depends(provider_auth)):
    return identity
```

`verify_with_provider_keys` is a placeholder for your provider library's verified validation function. Return `None` when verification fails; return only the identity claims your app needs. The client calls this route with `Authorization: Bearer <provider_access_token>`. `auth=` enforces the check, and `Depends(provider_auth)` injects the verified identity. Add authorization checks for roles or permissions separately.

`SECURITY_STORAGE` selects the shared store used by sessions, opaque bearer tokens, IP flags, and HMAC replay checks. Its async methods include `get(key)`, `set(key, value, ttl=...)`, `delete(key)`, and atomic `claim(key, ttl)`. The generated project defaults to database storage. Database storage needs the framework migration; Redis requires a running Redis server and the Redis extra. Memory storage is process-local and is not shared across workers.

## Social login

`SocialAuth` is the managed browser sign-in flow. It differs from `oidc_auth()`, which verifies a token already presented to an API route. `SocialAuth` generates provider login and callback routes for Google, GitHub, and LinkedIn. X and Facebook are temporarily disabled; if either is listed in `providers`, it is ignored, so no login or callback route is generated for it. Other unknown provider names remain configuration errors. Configure each available provider's client ID, client secret, and exact callback URI in the generated `settings.py` (or load them from environment variables). The generated settings leave credentials blank. Register each callback URI with the matching provider.

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

This mounts routes such as `/auth/google/login` and `/auth/google/callback` only on the app that includes the router. The sign-in flow is:

1. The browser visits `/auth/<provider>/login` and is redirected to the identity provider.
2. The provider authenticates the person and redirects the browser to the registered callback URL.
3. LenAPI validates OAuth state (and PKCE where supported), exchanges the authorization code, and fetches the provider profile.
4. With `auto_register=True`, LenAPI finds or creates the local user and stores the provider-to-user mapping.
5. With `jwt_enabled=True`, LenAPI issues **one LenAPI JWT access token** for use on protected application routes.

`jwt_enabled` controls only that final LenAPI credential. SocialAuth currently issues a JWT access token; it does not issue an API key, session cookie, opaque bearer token, or refresh token. The provider's OAuth access token is used internally during the callback and is not returned to the client. With `jwt_enabled=False`, the callback response contains the provider `identity` and includes `user_id` when a local user was resolved, but no LenAPI token. For sessions, API keys, or opaque tokens, implement the desired credential issuance after the provider callback using the matching LenAPI auth dependency/store, or build a custom callback flow. Google and LinkedIn use OIDC identity endpoints in this integration; GitHub uses OAuth 2.0 and its profile/email APIs, not OIDC.

For example, with both flags enabled, the callback response includes fields in this shape (identity claims vary by provider):

```json
{
  "identity": {"sub": "provider-subject", "email": "ada@example.com"},
  "user_id": 42,
  "access_token": "<LenAPI JWT>",
  "token_type": "bearer",
  "expires_in": 3600
}
```

The client then sends `Authorization: Bearer <access_token>` to routes protected with LenAPI JWT authentication. Store and transmit the token securely; do not use the provider token in place of the LenAPI token.

`allowed_origins` can also be passed directly to `SocialAuth`; otherwise it reads `SOCIAL_AUTH_ALLOWED_ORIGINS` from `settings.py`. When a browser sends an `Origin` header to a provider login route, LenApi requires an exact match in this list and returns `403` otherwise. Replace `https://myapp.com` with the exact origin serving your frontend. Top-level navigations can omit `Origin`; those still rely on the required, random, browser-cookie-bound OAuth `state` check on callback. Do not treat the origin allowlist as a replacement for OAuth state validation, HTTPS, or correct provider callback URL registration. The provider callback itself is not checked against the browser `Origin` header because providers may omit it or send a different origin.

With `auto_register=True`, LenApi creates a framework `User` (or an `app.models.User` subclass) on the first successful provider login and stores the provider subject mapping in the framework-managed `lenapi_social_accounts` table. Run `python lenora.py migration` and `python lenora.py migrate` before first use. Only verified provider emails are copied to the local user. If a verified email is already attached to a different account, sign-in returns `409`; LenApi will not silently link accounts by email.

The scaffold includes blank settings for `GOOGLE_*`, `GITHUB_*`, and `LINKEDIN_*` client ID, client secret, redirect URI, and cookie security values. Keep client secrets in environment variables or a local ignored `.env` file.

Use `SocialAuth` for the built-in Google, GitHub, and LinkedIn authorization-code flows; retain a custom flow when your provider or account-linking rules need different behavior.

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

The generated `SECRET_KEY` is unique to each new project and signs JWTs. The framework-backed Basic and JWT credential check looks up `User` in the database and verifies its hashed `password`. Use `make_password(password)` to hash a password and `check_password(password, encoded_password=stored_password)` to verify it; the latter returns a boolean. Never store plaintext passwords. The older `hash_password()` and `verify_password()` names remain available as aliases. Use HTTPS for Basic credentials and bearer/API-key requests outside local development.

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

For API-key mode, call `register_user(data=..., authentication="api_key")` from your own registration route. It generates a URL-safe key with 32 bytes of cryptographically secure randomness and returns it once as plain text. LenAPI stores only its SHA-256 digest in the nullable `user.api_key` column, so the original key cannot be recovered later. Send the returned key on authenticated requests with `X-API-Key: <key>`. The `python lenora.py apikey` command also creates an `APIKey` table for app-managed key records, but the built-in authenticator looks up `User.api_key`; the helper does not automatically add rows to that separate table.

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

The stored path is relative. New projects use `MEDIA_URL = "/"`, so a file saved directly into `MEDIA_ROOT` with `await image.save()` is available at `/<generated-filename>.jpg` (for example, `https://example.com/755cdb37768845ad976f9ddacf308173.jpg`). Root-level media serving only handles an existing file whose URL has one path segment; if there is no matching file, normal application routes continue to be checked. Save with a directory such as `await image.save("profiles")` to keep the file under that folder; that stored path is `/profiles/<filename>.jpg` when `MEDIA_URL = "/"`. Existing projects can retain `MEDIA_URL = "/media/"` and their current `/media/...` URLs. Set `MEDIA_ROOT` and `MEDIA_URL` in `settings.py`; media paths are restricted to files inside `MEDIA_ROOT`, directory listings are not exposed, and non-image uploads are sent as attachments. Request body size limits apply to multipart uploads too.

To publish a generated image directly at the domain root, save it without a subdirectory and store the returned filename in the model:

```python
@app.post("/students/{student_id}/profile-image")
async def upload_profile_image(student_id: int, image: UploadFile = File(...)):
    filename = await image.save()  # MEDIA_ROOT/<generated-name>.jpg
    student = Student.get_or_404(id=student_id)
    student.profile_image = filename
    student.save()
    return {
        "student_id": student.id,
        "profile_image": student.profile_image,
        "image_url": f"/{filename}",
    }
```

## Webhooks and background work

Webhook senders retry deliveries, so verify the provider signature and deduplicate with its event ID. `webhook_auth()` verifies an HMAC-SHA256 signature over the exact raw body. If a timestamp header is configured, it verifies freshness and signs `timestamp + b"." + body`. Give the dependency the shared security store to acknowledge duplicate event IDs with HTTP 200 without running the route handler again:

```python
from typing import Any
from lenapi import BackgroundTasks, Body, Depends
from lenapi.security import webhook_auth

webhook = webhook_auth(
    settings.WEBHOOK_SECRET,
    security_store=app.security_store,
    provider="billing",
    signature_header="x-webhook-signature",
    event_id_header="x-webhook-id",
    timestamp_header="x-webhook-timestamp",
)

def process_event(event_id, payload):
    # Process the event; open a fresh database session here if needed.
    ...

@app.post("/webhooks/billing")
async def billing_webhook(
    payload: Any = Body(),
    verified=Depends(webhook),
    background_tasks: BackgroundTasks = None,
):
    background_tasks.add_task(process_event, verified["event_id"], payload)
    return {"received": True}
```

`BackgroundTasks.add_task(function, *args, **kwargs)` runs work after lenApi sends the response. Synchronous functions run in a worker thread; async functions are awaited. This built-in runner is in-process and best for short follow-up work. A process crash can lose queued work, so use a durable queue for jobs that must survive restarts. The webhook dependency claims an event ID for seven days by default; configure `deduplication_ttl` if the provider retries for a different period. If the security store is database-backed, run `python lenora.py migration` first.

## Databases

`DATABASE_TYPE` chooses one backend and `DATABASE_URL` connects to it. The generated project defaults to SQLite. SQLite, MySQL, and PostgreSQL use SQLAlchemy models and Alembic revisions; MongoDB uses the separate `MongoDocument` API and does not have SQL schema revisions. `app.database_type` reports the selected backend at runtime. `ACTIVATE_ASYNC = True` enables async access to the same database URL. Keep production credentials out of committed settings and URL-encode reserved characters in credentials.

```python
# settings.py — choose one backend and its URL
DATABASE_TYPE = "sqlite"
DATABASE_URL = f"sqlite:///{BASE_DIR / 'lenapi.db'}"

# MySQL: install `python -m pip install 'lenapi[mysql]'`
# DATABASE_TYPE = "mysql"
# DATABASE_URL = "mysql://user:password@localhost:3306/app_db"

# PostgreSQL: install `python -m pip install 'lenapi[postgres]'`
# DATABASE_TYPE = "postgresql"
# DATABASE_URL = "postgresql://user:password@localhost:5432/app_db"

# MongoDB: install `python -m pip install 'lenapi[mongodb]'`
# DATABASE_TYPE = "mongodb"
# DATABASE_URL = "mongodb://user:password@localhost:27017/app_db"
# If the URI has no database path, set this explicitly instead:
# MONGODB_DATABASE = "app_db"

ACTIVATE_ASYNC = False
```

LenAPI chooses the supported synchronous driver for a selected MySQL or PostgreSQL URL. Async access uses `aiosqlite` for SQLite; install `lenapi[async-mysql]` for MySQL or `lenapi[async-postgres]` for PostgreSQL. MongoDB uses its separate sync/async document API.

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

`MongoDatabase.ping()` and `await MongoDatabase.aping()` check connectivity. To select MongoDB, set `DATABASE_TYPE = "mongodb"`. Put the database name in the URI path, such as `mongodb://localhost:27017/my_database`, or set `MONGODB_DATABASE = "my_database"` when the URI has no path. `ACTIVATE_ASYNC` controls whether LenApi also creates PyMongo's async client. `migration` and `migrate` ensure collections exist for imported `MongoDocument` models and framework security/admin state; document fields are schema-flexible and do not use Alembic revisions. `SECURITY_STORAGE = "database"` uses MongoDB for shared security state, while Redis and process memory remain optional choices. MongoDB uses the official synchronous and `AsyncMongoClient` APIs; see the [PyMongo driver guide](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/).

#### Switching an existing project from SQL to MongoDB

Changing `DATABASE_TYPE` and `DATABASE_URL` selects MongoDB for the app; it does not convert existing SQLAlchemy models, tables, or rows. Define the Mongo-backed version as a `MongoDocument`, bind it after creating the app, and update routes to use that document class. If a SQL `model.Model` query runs while MongoDB is selected, LenAPI raises a clear backend-mismatch error explaining that a Mongo document is required. Check the active backend with `app.database_type`.

```python
# settings.py
DATABASE_TYPE = "mongodb"
DATABASE_URL = "mongodb://127.0.0.1:27017/my_app"

# app/models.py
from lenapi import MongoDocument

class MongoStudent(MongoDocument):
    collection_name = "students"
    username: str
    course: str

# main.py
from lenapi import LENAPI
from pydantic import BaseModel
from app.models import MongoStudent

class StudentCreate(BaseModel):
    username: str
    course: str

app = LENAPI()
MongoStudent.bind(app.mongodb)
print(app.database_type)  # mongodb

@app.post("/students")
def create_student(data: StudentCreate):
    return MongoStudent.create(**data.model_dump())
```

If you need to carry existing records over, treat that as an explicit data migration: back up both stores, copy in controlled batches, verify counts and important fields, and only then switch production traffic. LenAPI's `migration`/`migrate` commands initialize MongoDB collections and framework state; they do not copy SQL data. MongoDB document IDs are strings, so update route and response-schema ID types that previously expected integer SQL IDs.

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

`model.Model` is the base class for SQL-backed models. Subclass it to declare a table; each field assignment declares a database column, and the framework supplies an integer `id` primary key unless you define one yourself. Field declarations describe storage and database constraints; use Pydantic schemas to validate API input.

The field helpers include `AutoField`, signed `IntegerField` and `BigIntegerField`/`BigIntField`, nonnegative `PositiveIntegerField` and `PositiveBigIntegerField`, `StringField`, `TextField`, `EmailField`, `PhoneField`, `IPField`, `URLField`, `BooleanField`, `FloatField`, `DecimalField`, `DateField`, `TimeField`, `DateTimeField`, `DurationField`, `JSONField`/`Json`, `UUIDField`, `ImageField`, `FileField`, `FileImageField`, `ForeignKeyField`, `OneToOneField`, and `ManyToManyField`. Fields normally default to `nullable=True` and `blank=True`; `BooleanField` defaults to `True`. `JSONField` and its alias `Json` store JSON-compatible Python values. Use a factory such as `default=dict` for a fresh mutable value on each new row:

```python
class Student(model.Model):
    username = model.StringField(max_length=120, unique=True)
    profile = model.Json(default=dict)

student = Student.create(
    username="ada",
    profile={"course": "Python", "active": True},
)
```

`DateField`, `TimeField`, and `DateTimeField` default to the current UTC date or time when no value or explicit default is supplied. Pass `auto_add_now=False` to disable this behavior, or provide your own `default`. `DurationField(weeks=1)` stores a fixed seven-day `timedelta`; add it to a timestamp when calculating an expiry date. Months and years are calendar intervals and should use calendar-aware date arithmetic.

`PositiveIntegerField` and `PositiveBigIntegerField` reject negative values in LenAPI and add a named database `CHECK` constraint, so direct SQL writes are protected too. `EmailField`, `PhoneField`, `IPField`, `URLField`, and `UUIDField` validate values when a model instance is saved. `PhoneField` accepts a leading `+` followed by country code and digits, up to 18 characters total. `IPField` accepts IPv4 and IPv6. `URLField` accepts `http://`, `https://`, and `www.` URLs. Customize messages with `error_messages`, for example `email = model.EmailField(error_messages={"email": "This is not an email"})`; `unique` is also a supported message key for a unique field.

The base model supplies an auto-incrementing integer `id`. A model may declare its own primary key instead; LenAPI will not add a second `id` key:

```python
class ExternalRecord(model.Model):
    external_id = model.StringField(max_length=40, primary_key=True)
    label = model.StringField(max_length=120)
```

File model fields describe what kind of path is stored. The matching upload mode enforces the type: `ImageField` uses `await upload.save("profiles", type="image")`, `FileField` uses `await upload.save("documents", type="file")` and rejects image uploads, and `FileImageField` uses `type="file_image"` when either kind is allowed. Pass `max_size` in bytes to enforce a per-file size limit. Continue to store the returned relative path in the model; upload files through an `UploadFile` route parameter so Swagger presents a multipart file picker.

```python
class Student(model.Model):
    profile_image = model.ImageField(upload_to="profiles", nullable=True)
    attachment = model.FileField(upload_to="documents", nullable=True)
    mixed_upload = model.FileImageField(upload_to="uploads", nullable=True)

@app.post("/students/{student_id}/attachment")
async def upload_attachment(student_id: int, file: UploadFile = File(...)):
    student = Student.get_or_404(id=student_id)
    filename = await file.save("documents", type="file", max_size=10 * 1024 * 1024)
    student.attachment = f"documents/{filename}"
    student.save()
    return {"attachment": student.attachment}
```

`unique=True` is a database constraint that prevents duplicate values in that column. Constraint names are generated automatically; explicit names are optional. `Model.get.unique("field")` is a query projection that selects a field while looking up a row. It does not add a uniqueness constraint to the schema. Put `unique=True` on the field when the database itself must guarantee that only one row can have that value.

Constraint names are optional in model declarations. LenApi automatically assigns stable names to generated primary-key, foreign-key, index, and unique constraints, so a normal `unique=True` field needs no explicit name and works with Alembic, including SQLite batch migrations. Use `__table_args__` only when you want to choose the exact unique-constraint name; omit `unique=True` on that field to avoid creating a second constraint:

```python
class Student(model.Model):
    phone = model.PhoneField(max_length=18, nullable=False, unique=True)

# To choose the exact SQL constraint name instead:
class StudentWithNamedPhone(model.Model):
    phone = model.PhoneField(max_length=18, nullable=False)
    __table_args__ = (
        model.UniqueConstraint("phone", name="uq_student_phone"),
    )

class Enrollment(model.Model):
    student_id = model.ForeignKeyField(
        "student.id",
        constraint_name="fk_enrollment_student_id_student",
    )
```

`ForeignKeyField.constraint_name` is also optional; set it only when you need a specific foreign-key name.

### Migration commands

Run the commands from the generated project root. Import application models before migration discovery, normally by importing them in `main.py`.

`migration` detects schema changes against imported model metadata, creates a new Alembic revision if needed, and applies pending revisions. It does not upgrade the database when there is no new revision. Use it during development after changing relational models:

```bash
python lenora.py migration
python lenora.py migration --message "add student phone"
```

An existing unversioned SQLite database is adopted only when its schema matches the migration history; adoption preserves its data.

`PositiveIntegerField` and `PositiveBigIntegerField` include a named database `CHECK` constraint. LenAPI prevents Alembic's metadata-copy step from adding that constraint twice, which MySQL rejects as a duplicate constraint name. If a local revision generated by an older LenAPI contains duplicate identical `sa.CheckConstraint(...)` entries, correct or regenerate that unapplied revision before using it. MySQL uses non-transactional DDL, so a failed migration may leave some project tables created even though no revision was recorded. Check `showmigrations` and inspect the project-namespaced tables before retrying. For a disposable database, `delete_migrations` can reset this project's tables and revisions after its confirmation prompt; if the data must be preserved, reconcile the partial schema instead of deleting it.

`migrate` applies pending migration files already in `migrations/versions/`; it does not generate a revision. A common release workflow is to generate and inspect a revision during development, commit the revision with the code, then run `migrate` during deployment:

```bash
# Development
python lenora.py migration

# Deployment after the revision file is checked in
python lenora.py migrate
```

`showmigrations` reports local revision files and whether each is applied. It also flags revision IDs that the database or local revision chain references but whose files are missing:

```bash
python lenora.py showmigrations
```

`repair-migrations` repairs only migration history. It compares the live SQL schema with the current models, then stamps the local head only when they match; it preserves schema and data and refuses mismatches. If a revision file is missing, restore it when possible; use repair only after confirming the live schema matches current model metadata. On an empty database with no revisions, use `migration` to create a new initial revision instead. MongoDB has no Alembic history and does not support this repair command.

```bash
python lenora.py repair-migrations
```

`clear_migrations` removes local revision files and this project's Alembic version table only when there are no application tables in this project's namespace. It is for resetting an empty SQL project, not for reconciling an existing schema.

`delete_migrations` is a destructive reset of the current project. After an interactive y/n confirmation, it drops tables and views in this project's SQL namespace (or collections in this project's MongoDB namespace) and deletes local revision files. Objects belonging to other project namespaces are left alone. This also deletes this project's application data. Back up first and use it only when a clean project database is intended:

```bash
python lenora.py delete_migrations
```

For MongoDB, `migration` and `migrate` initialize missing framework and imported document collections; they do not create Alembic revisions or migrate document fields. `showmigrations` reports that MongoDB has no Alembic revisions; `repair-migrations` and `clear_migrations` are SQL-only; `delete_migrations` drops all collections after confirmation.

Inside a request, models provide these query helpers:

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

### CRUD, search, and pagination: sync and async

Each sync operation has an async counterpart. Sync calls return immediately; async calls must be awaited inside an `async def` handler, with `ACTIVATE_ASYNC = True` in settings.

```python
# 1. Create
student = Student.create(person_name="Ada", course="Python")
async_student = await Student.acreate(person_name="Ada", course="Python")

# 2. Get all rows
students = Student.get.all()
async_students = await Student.aget.all()

# 3. Get one row by a unique value or primary key
student = Student.get(id=1).get()
async_student = await Student.aget(id=1)

# Check whether a row exists
exists = Student.get(id=1).exists()
async_exists = await Student.aget(id=1).exists()

# 4. Select only particular columns while applying lookup filters
name = Student.get("person_name").get(id=1)  # scalar value
summary = Student.get("person_name", "course").get(id=1)  # mapping
async_summary = await Student.afilter().values(
    "person_name", "course"
).get(id=1)

# 5. Filter rows. Chain values/order/limit/offset before all().
python_students = Student.filter(course__iexact="python").all()
page = Student.filter(course__icontains="py").values(
    "person_name", "course"
).order("-id").limit(20).offset(0).all()
async_page = await Student.afilter(course__icontains="py").values(
    "person_name", "course"
).order("-id").all(limit=20, offset=0)

# 6. Search string fields (all search terms must match)
matches = Student.search(person_name="ada", course="python").all()
async_matches = await Student.asearch(person_name="ada").all()

# Search one term across specified columns (any column may match)
matches_any = Student.search_any(
    "python", "person_name", "course"
).all()
async_matches_any = await Student.asearch_any(
    "python", "person_name", "course"
).all()

# 7. Pagination metadata accompanies list-like results
page = Student.filter(course="Python").all(limit=20, offset=40)
total = page.total_count
pages = page.page_count
metadata = page.pagination

async_page = await Student.afilter(course="Python").all(limit=20, offset=40)
async_total = async_page.total_count
async_pages = async_page.page_count
```

`Student.get.all()` returns all rows synchronously; its async counterpart is `await Student.aget.all()`. For filtered async lists, use `await Student.afilter(...).all()`. `Student.get(...)` builds a single-row lookup; call `.get()` for the model instance or `.exists()` for a boolean. The async single-row counterpart is `await Student.aget(...)`, and its existence check is `await Student.aget(...).exists()`. Multiple matches are an error rather than an arbitrary row selection. `Student.get("field", ...).get(...)` selects one or more columns; one selected column returns a scalar and multiple columns return a mapping. This is a query projection, not a uniqueness constraint. To make the database reject duplicates, declare `unique=True` on the field.

### Fetch related records

With a `ForeignKeyField` such as `Student.classroom_id`, sync code can resolve the target row using `.special("classroom_id")`. It returns a proxy where that same attribute contains the related `Classroom` instance (or `None` if the foreign key is null):

```python
from app.models import Classroom, Student

student = Student.get(id=1).special("classroom_id")
classroom = student.classroom_id
if classroom is not None:
    print(classroom.name)
```

For async code, use the stored foreign-key ID for a second async lookup. `.special()` is a sync helper:

```python
student = await Student.aget(id=1)
classroom = None
if student is not None and student.classroom_id is not None:
    classroom = await Classroom.aget(id=student.classroom_id)
```

For `ManyToManyField`, the related collection is available as an attribute. In sync code, call `.all()` to get its related model instances:

```python
from app.models import Teacher

teacher = Teacher.get(id=1).get()
courses = teacher.courses.all()
print([course.name for course in courses])
```

In async routes, eager-load the collection with SQLAlchemy's `selectinload` and the injected `AsyncSession` before reading it. Direct lazy access would try to perform database I/O during ordinary attribute access:

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from lenapi import HTTPException
from app.models import Teacher

@app.get("/teachers/{teacher_id}/courses")
async def teacher_courses(teacher_id: int, db: AsyncSession):
    result = await db.scalars(
        select(Teacher)
        .options(selectinload(Teacher.courses))
        .where(Teacher.id == teacher_id)
    )
    teacher = result.one_or_none()
    if teacher is None:
        raise HTTPException(404, "Teacher not found")
    return [{"id": course.id, "name": course.name}
            for course in teacher.courses.all()]
```

Declare the relationships in `app/models.py` and run `python lenora.py migration` before querying them. Async relationship loading requires `ACTIVATE_ASYNC = True` and the matching async driver.

`filter(...)` accepts equality filters and lookups such as `course__icontains="py"`, `id__gte=10`, `id__in=[1, 2]`, and `classroom_id__isnull=True`. Supported lookup suffixes are `exact`, `iexact`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `notin`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, and `isnull`. Query methods include `.values(...)`, `.order(field)` (prefix with `-` for descending), `.limit(n)`, `.offset(n)`, and terminal operations `.all()`, `.first()`, `.one_or_none()`, `.count()`, `.aggregate(...)`, and `.delete()`. Async query builders use `afilter()`, `asearch()`, and `asearch_any()`; await terminal methods such as `.all()`, `.get()`, `.count()`, `.aggregate()`, or `.delete()`.

`search(**terms)` applies case-insensitive substring matching and combines supplied terms with AND. `search_any(query, *fields)` searches one term across string columns and combines fields with OR. Paginated results remain list-like and expose `.total_count`, `.page_count`/`.total_pages`, and `.pagination`; `limit` and `offset` must be non-negative integers. Annotate a route argument as SQLAlchemy's `Session` when you need transaction control directly.

Use a Pydantic output schema for paginated API responses so Swagger shows both each student and the pagination metadata:

```python
from typing import Any
from pydantic import BaseModel, ConfigDict
from lenapi import LENAPI
from app.models import Student

app = LENAPI()

class StudentOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    person_name: str | None
    course: str | None

class StudentPageOutput(BaseModel):
    results: list[StudentOutput]
    pagination: dict[str, Any] | None

@app.get("/students", response_model=StudentPageOutput)
def list_students(limit: int = 20, offset: int = 0):
    page = Student.filter(course="Python").all(limit=limit, offset=offset)
    return {
        "results": [StudentOutput.model_validate(row).model_dump(mode="json")
                    for row in page],
        "pagination": page.pagination,
    }

@app.get("/async/students", response_model=StudentPageOutput)
async def list_students_async(limit: int = 20, offset: int = 0):
    page = await Student.afilter(course="Python").all(
        limit=limit, offset=offset
    )
    return {
        "results": [StudentOutput.model_validate(row).model_dump(mode="json")
                    for row in page],
        "pagination": page.pagination,
    }
```

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

Use `OneToOneField` when a dependent row must have exactly one owner and the owner may have at most one dependent row. It is a foreign key with a database unique constraint. A one-to-many relation uses an ordinary `ForeignKeyField` on the many-side; query matching rows with `filter()`:

```python
class Classroom(model.Model):
    name = model.StringField(max_length=120)

class Learner(model.Model):
    name = model.StringField(max_length=120)
    classroom_id = model.ForeignKeyField("classroom.id", nullable=False)

class LearnerProfile(model.Model):
    learner_id = model.OneToOneField("learner.id", nullable=False)
    biography = model.TextField()

classroom = Classroom.get_or_404(id=1)
learners = Learner.filter(classroom_id=classroom.id).all()
learner = Learner.get_or_404(id=1).special("classroom_id")
print(learner.classroom_id.name)

profile = LearnerProfile.get(learner_id=learner.id).get()
```

For many-to-many queries, load the owning model and access its set-like collection. With async routes, await model query methods; use eager loading with SQLAlchemy `selectinload()` when traversing async relationships.

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
- Swagger UI at `/docs/`, ReDoc at `/redoc/`, and generated OpenAPI 3.1 at `/openapi.json`
- JSON responses by default, with `Response` and `JSONResponse` for explicit control
- Request cookie parsing through `request.cookies`, plus `Response.set_cookie()` and `Response.delete_cookie()`

The installable library lives in `lenapi/`; runnable applications and their migrations are kept separately under `app/`. The standalone wheel and source download contain the framework, packaging metadata, documentation, and library tests, but omit application code and local databases.

## License

LenAPI is distributed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full license text.

## Sponsors

LenAPI appreciates the organizations supporting the project.

<p align="center">
  <a href="https://justjobng.online"><img src="assets/sponsors/justjob.webp" alt="JustJob" height="76"></a>&nbsp;&nbsp;
  <a href="https://www.letsmeet.date/"><img src="assets/sponsors/letsmeet.jpeg" alt="LetsMeet" height="76"></a>&nbsp;&nbsp;
  <a href="https://www.litregreprediction.site/"><img src="assets/sponsors/litregre.png" alt="Litregre Prediction" height="76"></a>&nbsp;&nbsp;
  <a href="https://maekandex.com"><img src="assets/sponsors/maekandex.jpeg" alt="Maekandex" height="76"></a>
</p>
