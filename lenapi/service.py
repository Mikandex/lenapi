"""Optional services for WebSockets, caching, background jobs, and AI APIs."""

import asyncio
import inspect
import json
import math
import threading
import time
from typing import Any
from urllib.parse import parse_qs

from .dependencies import Depends
from .request import Request
from .response import HTTPException


class WebSocketDisconnect(Exception):
    """Raised when a WebSocket peer disconnects during a receive operation."""

    def __init__(self, code: int = 1000) -> None:
        super().__init__(f"WebSocket disconnected with code {code}")
        self.code = code


class WebSocket:
    """Convenient send and receive methods for an ASGI WebSocket connection."""

    def __init__(self, scope: dict[str, Any], receive, send, *, path_params=None) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path_params = path_params or {}
        self.accepted = False
        self.closed = False
        self.subprotocol: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in self.scope.get("headers", [])
        }

    @property
    def query_string(self) -> str:
        return self.scope.get("query_string", b"").decode("latin-1")

    async def accept(self, subprotocol: str | None = None) -> None:
        if self.closed:
            raise RuntimeError("Cannot accept a closed WebSocket")
        if self.accepted:
            return
        self.subprotocol = subprotocol
        message = {"type": "websocket.accept"}
        if subprotocol is not None:
            message["subprotocol"] = subprotocol
        await self._send(message)
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        if self.closed:
            raise WebSocketDisconnect()
        message = await self._receive()
        if message["type"] == "websocket.disconnect":
            self.closed = True
            raise WebSocketDisconnect(message.get("code", 1000))
        if message["type"] != "websocket.receive":
            raise RuntimeError(f"Unexpected ASGI WebSocket message: {message['type']}")
        return message

    async def receive_text(self) -> str:
        message = await self.receive()
        text = message.get("text")
        if text is not None:
            return text
        data = message.get("bytes")
        if data is not None:
            return data.decode("utf-8")
        raise ValueError("WebSocket message did not contain text or bytes")

    async def receive_bytes(self) -> bytes:
        message = await self.receive()
        data = message.get("bytes")
        if data is not None:
            return data
        text = message.get("text")
        if text is not None:
            return text.encode("utf-8")
        raise ValueError("WebSocket message did not contain text or bytes")

    async def receive_json(self) -> Any:
        return json.loads(await self.receive_text())

    async def send_text(self, data: str) -> None:
        self._ensure_accepted()
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        self._ensure_accepted()
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any) -> None:
        await self.send_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    async def send(self, data: Any) -> None:
        """Send text, bytes, or a JSON-compatible value."""
        if isinstance(data, bytes):
            await self.send_bytes(data)
        elif isinstance(data, str):
            await self.send_text(data)
        else:
            await self.send_json(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.closed:
            return
        message: dict[str, Any] = {"type": "websocket.close", "code": code}
        if reason is not None:
            message["reason"] = reason
        await self._send(message)
        self.closed = True

    async def disconnect(self, code: int = 1000, reason: str | None = None) -> None:
        """Close this connection; alias for :meth:`close`."""
        await self.close(code=code, reason=reason)

    def _ensure_accepted(self) -> None:
        if not self.accepted or self.closed:
            raise RuntimeError("Accept the WebSocket before sending data")


class WebSocketConnection:
    """One connected client managed by :class:`WebSocketService`."""

    def __init__(self, service: "WebSocketService", websocket: WebSocket,
                 room: str, identity: Any = None) -> None:
        self.service = service
        self.websocket = websocket
        self.room = room
        self.identity = identity

    async def send_text(self, data: str) -> None:
        await self.websocket.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.websocket.send_bytes(data)

    async def send_json(self, data: Any) -> None:
        await self.websocket.send_json(data)

    async def send(self, data: Any) -> None:
        """Send text, bytes, or a JSON-compatible value to this client."""
        await self.websocket.send(data)

    async def broadcast_text(self, data: str) -> None:
        await self.service._broadcast(self.room, "text", data)

    async def broadcast_bytes(self, data: bytes) -> None:
        await self.service._broadcast(self.room, "bytes", data)

    async def broadcast_json(self, data: Any) -> None:
        await self.service._broadcast(self.room, "json", data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await self.websocket.close(code=code, reason=reason)

    async def disconnect(self, code: int = 1000, reason: str | None = None) -> None:
        await self.websocket.disconnect(code=code, reason=reason)


class WebSocketService:
    """Ready-to-mount WebSocket chat and signaling service.

    ``type`` selects the accepted message shape: ``chat``, ``video``,
    ``chat_video``, ``text_message``, ``json_message``, or ``bytes``. Chat,
    video, and combined modes relay JSON dictionaries between clients sharing
    a room. A room can be selected by a ``{room_id}`` path parameter or a
    ``?room=...`` query parameter. This built-in relay uses process-local
    memory; multi-worker deployments need a shared pub/sub backend.
    """

    _TYPES = {"chat", "video", "chat_video", "text_message", "json_message", "bytes"}
    _ALIASES = {
        "chat&video": "chat_video",
        "text": "text_message",
        "message": "text_message",
        "json": "json_message",
        "byte": "bytes",
        "binary": "bytes",
    }

    def __init__(
        self,
        *,
        path: str,
        type: str = "chat",
        auth=None,
        allowed_origins: list[str] | tuple[str, ...] | None = None,
        max_message_size: int = 64_000,
        max_connections: int | None = None,
        max_total_connections: int | None = 100,
        heartbeat_seconds: float | None = 30,
    ) -> None:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must be an absolute WebSocket path")
        if not isinstance(type, str) or not type.strip():
            raise ValueError("type must be a non-empty string")
        message_type = self._ALIASES.get(type.strip().lower(), type.strip().lower())
        if message_type not in self._TYPES:
            choices = "chat, video, json, message, byte (also chat_video)"
            raise ValueError(f"Unsupported WebSocket type '{type}'. Choose from: {choices}")
        if isinstance(max_message_size, bool) or not isinstance(max_message_size, int) or max_message_size <= 0:
            raise ValueError("max_message_size must be a positive integer")
        if max_connections is not None and (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or max_connections <= 0
        ):
            raise ValueError("max_connections must be a positive integer or None")
        if max_total_connections is not None and (
            isinstance(max_total_connections, bool)
            or not isinstance(max_total_connections, int)
            or max_total_connections <= 0
        ):
            raise ValueError("max_total_connections must be a positive integer or None")
        if heartbeat_seconds is not None and (
            isinstance(heartbeat_seconds, bool)
            or not isinstance(heartbeat_seconds, (int, float))
            or heartbeat_seconds <= 0
        ):
            raise ValueError("heartbeat_seconds must be positive or None")
        if auth is not None and not callable(auth) and not isinstance(auth, Depends):
            raise TypeError("auth must be a callable authentication dependency or Depends instance")
        origins = None if allowed_origins is None else tuple(allowed_origins)
        if isinstance(allowed_origins, str):
            raise TypeError("allowed_origins must be a list or tuple of origins")
        if origins is not None and any(not isinstance(origin, str) or not origin.strip() for origin in origins):
            raise ValueError("allowed_origins must contain non-empty origin strings")

        self.path = path
        self.type = message_type
        self.auth = auth
        self.allowed_origins = origins
        self.max_message_size = max_message_size
        self.max_connections = max_connections
        self.max_total_connections = max_total_connections
        self.heartbeat_seconds = float(heartbeat_seconds) if heartbeat_seconds is not None else None
        self._rooms: dict[str, set[WebSocketConnection]] = {}
        self._lock = asyncio.Lock()

    def router(self):
        """Return a Router with this service's WebSocket endpoint."""
        from .application import Router

        router = Router()
        router.websocket(self.path)(self._handle_connection)
        return router

    async def _authenticate(self, websocket: WebSocket):
        if self.auth is None:
            return None
        dependency = self.auth.dependency if isinstance(self.auth, Depends) else self.auth
        scope = dict(websocket.scope)
        scope.update({"type": "http", "method": "GET", "path": scope.get("path", self.path)})

        async def empty_body():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, empty_body, max_body_size=0, body_timeout=1)
        identity = dependency(request)
        if inspect.isawaitable(identity):
            identity = await identity
        if identity is None or identity is False:
            raise HTTPException(401, "WebSocket authentication failed")
        return identity

    def _room_name(self, websocket: WebSocket) -> str:
        path_room = websocket.path_params.get("room_id")
        query_room = parse_qs(websocket.query_string).get("room", [None])[0]
        return str(path_room or query_room or "default")

    async def _handle_connection(self, websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if self.allowed_origins is not None:
            if origin is None or ("*" not in self.allowed_origins and origin not in self.allowed_origins):
                await websocket.close(code=4403, reason="WebSocket origin is not allowed")
                return
        try:
            identity = await self._authenticate(websocket)
        except HTTPException as exc:
            code = 4403 if exc.status_code == 403 else 4401
            await websocket.close(code=code, reason=str(exc.detail)[:123])
            return

        room = self._room_name(websocket)
        connection = WebSocketConnection(self, websocket, room, identity)
        async with self._lock:
            active_connections = sum(len(peers) for peers in self._rooms.values())
            peers = self._rooms.get(room, set())
            connection_limit = self.max_connections
            if connection_limit is None and self.type in {"video", "chat_video"}:
                connection_limit = 2
            if self.max_total_connections is not None and active_connections >= self.max_total_connections:
                room_full = True
                existing_peers = []
                close_reason = "WebSocket connection limit reached"
                close_code = 1013
            elif connection_limit is not None and len(peers) >= connection_limit:
                room_full = True
                existing_peers = []
                close_reason = "Call room is full"
                close_code = 1008
            else:
                room_full = False
                existing_peers = list(peers)
                await websocket.accept()
                peers = self._rooms.setdefault(room, peers)
                peers.add(connection)
        if room_full:
            await websocket.close(code=close_code, reason=close_reason)
            return
        if self.type in {"video", "chat_video"}:
            if existing_peers:
                for peer in existing_peers:
                    try:
                        await peer.send_json({"type": "peer_joined"})
                    except (WebSocketDisconnect, OSError, RuntimeError):
                        pass
                await websocket.send_json({"type": "joined"})
            else:
                await websocket.send_json({"type": "waiting"})
        try:
            while True:
                message = await self._receive_message(websocket)
                if message is None:  # heartbeat response
                    continue
                kind, payload = message
                await self._broadcast(room, kind, payload)
        except WebSocketDisconnect:
            pass
        except (ValueError, TypeError, json.JSONDecodeError):
            await websocket.close(code=1003, reason="Invalid WebSocket message")
        finally:
            async with self._lock:
                peers = self._rooms.get(room)
                if peers is not None:
                    peers.discard(connection)
                    remaining = list(peers)
                    if not peers:
                        self._rooms.pop(room, None)
                else:
                    remaining = []
            if self.type in {"video", "chat_video"}:
                for peer in remaining:
                    try:
                        await peer.send_json({"type": "peer_left"})
                    except (WebSocketDisconnect, OSError, RuntimeError):
                        pass

    async def _receive_message(self, websocket: WebSocket):
        try:
            if self.heartbeat_seconds is None:
                message = await websocket.receive()
            else:
                message = await asyncio.wait_for(websocket.receive(), timeout=self.heartbeat_seconds)
        except asyncio.TimeoutError:
            await websocket.send_json({"type": "lenapi.ping"})
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=self.heartbeat_seconds)
            except asyncio.TimeoutError:
                await websocket.close(code=1001, reason="WebSocket heartbeat timed out")
                raise WebSocketDisconnect(1001)

        data = message.get("bytes")
        text = message.get("text")
        size = len(data) if data is not None else len(text.encode("utf-8")) if text is not None else 0
        if size > self.max_message_size:
            await websocket.close(code=1009, reason="WebSocket message is too large")
            raise WebSocketDisconnect(1009)
        if text is not None and text == '{"type":"lenapi.pong"}':
            return None

        if self.type == "bytes":
            if data is None:
                raise TypeError("This WebSocket service accepts binary messages only")
            return "bytes", data
        if self.type == "text_message":
            if text is None:
                raise TypeError("This WebSocket service accepts text messages only")
            return "text", text
        if text is None:
            raise TypeError("This WebSocket service expects JSON text messages")
        payload = json.loads(text)
        if isinstance(payload, dict) and payload.get("type") == "lenapi.pong":
            return None
        if self.type in {"chat", "video", "chat_video"} and not isinstance(payload, dict):
            raise TypeError("Chat and video messages must be JSON objects")
        if self.type == "video" and payload.get("type") not in {"offer", "answer", "candidate"}:
            raise TypeError("Video messages must be WebRTC offer, answer, or candidate events")
        if self.type == "chat_video" and payload.get("type") not in {
            "chat.message", "offer", "answer", "candidate"
        }:
            raise TypeError("Combined chat/video messages must be chat or WebRTC signaling events")
        return "json", payload

    async def send(self, room_id: str, data: Any) -> int:
        """Send data to all connected clients in a room; return delivered count."""
        if not isinstance(room_id, str) or not room_id:
            raise ValueError("room_id must be a non-empty string")
        if self.type == "bytes":
            if not isinstance(data, bytes):
                raise TypeError("This WebSocket service accepts binary messages only")
            kind, payload = "bytes", data
            size = len(data)
        elif self.type == "text_message":
            if not isinstance(data, str):
                raise TypeError("This WebSocket service accepts text messages only")
            kind, payload = "text", data
            size = len(data.encode("utf-8"))
        else:
            if self.type in {"chat", "video", "chat_video"} and not isinstance(data, dict):
                raise TypeError("Chat and video messages must be JSON objects")
            if self.type == "video" and data.get("type") not in {"offer", "answer", "candidate"}:
                raise TypeError("Video messages must be WebRTC offer, answer, or candidate events")
            if self.type == "chat_video" and data.get("type") not in {
                "chat.message", "offer", "answer", "candidate"
            }:
                raise TypeError("Combined chat/video messages must be chat or WebRTC signaling events")
            kind, payload = "json", data
            size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > self.max_message_size:
            raise ValueError("WebSocket message is too large")
        return await self._broadcast(room_id, kind, payload)

    async def disconnect(
        self, room_id: str, *, code: int = 1000, reason: str | None = None
    ) -> int:
        """Disconnect all clients in a room and return the number addressed."""
        if not isinstance(room_id, str) or not room_id:
            raise ValueError("room_id must be a non-empty string")
        async with self._lock:
            peers = list(self._rooms.get(room_id, ()))
        await asyncio.gather(
            *(peer.disconnect(code=code, reason=reason) for peer in peers),
            return_exceptions=True,
        )
        return len(peers)

    async def _broadcast(self, room: str, kind: str, payload: Any) -> int:
        async with self._lock:
            peers = list(self._rooms.get(room, ()))
        results = await asyncio.gather(
            *(self._send_to_peer(peer, kind, payload) for peer in peers),
            return_exceptions=True,
        )
        disconnected = [peer for peer, result in zip(peers, results) if isinstance(result, Exception)]
        if disconnected:
            async with self._lock:
                for peer in disconnected:
                    room_peers = self._rooms.get(room)
                    if room_peers is not None:
                        room_peers.discard(peer)
                        if not room_peers:
                            self._rooms.pop(room, None)
        return len(peers) - len(disconnected)

    @staticmethod
    async def _send_to_peer(peer: WebSocketConnection, kind: str, payload: Any) -> None:
        if kind == "text":
            await peer.send_text(payload)
        elif kind == "bytes":
            await peer.send_bytes(payload)
        else:
            await peer.send_json(payload)


class Faker:
    """Optional wrapper for generating realistic fake data.

    Install the integration with ``pip install 'lenapi[faker]'``. Faker
    provider attributes such as ``name``, ``email``, and ``address`` are
    delegated to the underlying Faker instance.
    """

    def __init__(self, *args, seed: int | float | str | bytes | None = None, **kwargs) -> None:
        try:
            from faker import Faker as FakerProvider
        except ImportError as exc:
            raise ImportError(
                "Faker service requires the optional dependency; from the lenApi project root run "
                "`python -m pip install -e '.[faker]'`."
            ) from exc
        self._provider = FakerProvider(*args, **kwargs)
        if seed is not None:
            self._provider.seed_instance(seed)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


class _AIHTTPService:
    """Shared optional HTTPX transport for async model-provider services."""

    def __init__(self, *, timeout: float, client=None) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return
        try:
            import httpx
        except ImportError as exc:
            raise ImportError("AI services require `pip install 'lenapi[ai]'`") from exc
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()


class OpenAI(_AIHTTPService):
    """Small async client for OpenAI chat-completion and embedding APIs.

    Install HTTPX with ``pip install 'lenapi[ai]'``. The API key defaults to
    the ``OPENAI_API_KEY`` environment variable.
    """

    def __init__(self, api_key: str | None = None, *, model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com/v1", timeout: float = 60, client=None) -> None:
        import os

        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("Pass api_key or set the OPENAI_API_KEY environment variable")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {self.api_key}"}
        super().__init__(timeout=timeout, client=client)

    async def chat(self, messages: list[dict[str, str]], *, model: str | None = None,
                   **options) -> dict:
        """Call OpenAI's chat-completions endpoint and return its JSON response."""
        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers,
            json={"model": model or self.model, "messages": messages, **options},
        )
        response.raise_for_status()
        return response.json()

    async def responses(self, input, *, model: str | None = None, **options) -> dict:
        """Call OpenAI's Responses API and return its JSON response."""
        response = await self._client.post(
            f"{self.base_url}/responses",
            headers=self._headers,
            json={"model": model or self.model, "input": input, **options},
        )
        response.raise_for_status()
        return response.json()

    async def embeddings(self, input: str | list[str], *, model: str = "text-embedding-3-small",
                         **options) -> dict:
        """Create embeddings and return OpenAI's JSON response."""
        response = await self._client.post(
            f"{self.base_url}/embeddings",
            headers=self._headers,
            json={"model": model, "input": input, **options},
        )
        response.raise_for_status()
        return response.json()


class Ollama(_AIHTTPService):
    """Small async client for a local or remote Ollama HTTP server."""

    def __init__(self, model: str = "llama3.2", *, base_url: str = "http://127.0.0.1:11434",
                 timeout: float = 120, client=None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        super().__init__(timeout=timeout, client=client)

    async def chat(self, messages: list[dict[str, str]], *, model: str | None = None,
                   options: dict | None = None, **parameters) -> dict:
        """Call Ollama's ``/api/chat`` endpoint without streaming."""
        body = {"model": model or self.model, "messages": messages, "stream": False}
        if options is not None:
            body["options"] = options
        body.update(parameters)
        response = await self._client.post(f"{self.base_url}/api/chat", json=body)
        response.raise_for_status()
        return response.json()

    async def generate(self, prompt: str, *, model: str | None = None,
                       options: dict | None = None, **parameters) -> dict:
        """Call Ollama's ``/api/generate`` endpoint without streaming."""
        body = {"model": model or self.model, "prompt": prompt, "stream": False}
        if options is not None:
            body["options"] = options
        body.update(parameters)
        response = await self._client.post(f"{self.base_url}/api/generate", json=body)
        response.raise_for_status()
        return response.json()

    async def embeddings(self, input: str | list[str], *, model: str | None = None,
                         **options) -> dict:
        """Create embeddings through Ollama's ``/api/embed`` endpoint."""
        response = await self._client.post(
            f"{self.base_url}/api/embed",
            json={"model": model or self.model, "input": input, **options},
        )
        response.raise_for_status()
        return response.json()


class Cache:
    """JSON-value cache with memory, database, and optional Redis backends."""

    def __init__(self, backend: str = "memory", *, engine=None, redis_url: str | None = None,
                 prefix: str = "cache:") -> None:
        self.backend = backend.lower()
        self.prefix = prefix
        self._values: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.RLock()
        self._store = None
        if self.backend == "database":
            if engine is None:
                raise ValueError("Database cache requires a configured SQLAlchemy engine")
            from .security_store import DatabaseSecurityStore
            self._store = DatabaseSecurityStore(engine)
        elif self.backend == "redis":
            if not redis_url:
                raise ValueError("Redis cache requires a redis_url")
            from .security_store import RedisSecurityStore
            self._store = RedisSecurityStore(redis_url)
        elif self.backend != "memory":
            raise ValueError("Cache backend must be 'memory', 'database', or 'redis'")

    async def get(self, key: str, default=None):
        """Return a decoded cached value, or ``default`` if missing or expired."""
        full_key = self.prefix + str(key)
        if self._store is not None:
            raw = await self._store.get(full_key)
            if raw is None:
                return default
            try:
                return json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return default
        now = time.monotonic()
        with self._lock:
            entry = self._values.get(full_key)
            if entry is None:
                return default
            expires_at, raw = entry
            if expires_at is not None and expires_at <= now:
                self._values.pop(full_key, None)
                return default
        return json.loads(raw)

    async def set(self, key: str, value, *, ttl: float | None = None) -> None:
        """Store a JSON-compatible value, optionally expiring after ``ttl`` seconds."""
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                                or not math.isfinite(ttl) or ttl <= 0):
            raise ValueError("ttl must be a positive number of seconds")
        raw = json.dumps(value, separators=(",", ":"))
        full_key = self.prefix + str(key)
        if self._store is not None:
            store_ttl = None if ttl is None else max(1, math.ceil(ttl))
            await self._store.set(full_key, raw, ttl=store_ttl)
            return
        expires_at = None if ttl is None else time.monotonic() + ttl
        with self._lock:
            self._values[full_key] = (expires_at, raw)

    async def delete(self, key: str) -> None:
        """Delete a cached key if it exists."""
        full_key = self.prefix + str(key)
        if self._store is not None:
            await self._store.delete(full_key)
            return
        with self._lock:
            self._values.pop(full_key, None)

    async def clear(self) -> None:
        """Clear the in-memory cache; shared stores require key-by-key deletion."""
        if self._store is not None:
            raise NotImplementedError("clear() is available only for the in-memory cache")
        with self._lock:
            self._values.clear()

    async def close(self) -> None:
        """Close the optional Redis connection pool."""
        if self.backend == "redis" and self._store is not None:
            await self._store.close()


# Keep service entry points together for projects using ``lenapi.service``.
from .background import BackgroundQueue, BackgroundTasks  # noqa: E402
from .payments import (  # noqa: E402
    Flutterwave,
    Nomba,
    PaymentPlugin,
    Payments,
    Paystack,
    Stripe,
    payment_provider,
    register_payment_provider,
)
