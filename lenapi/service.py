"""Optional services for WebSockets, caching, background jobs, and AI APIs."""

import json
import math
import threading
import time
from typing import Any


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

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.closed:
            return
        message: dict[str, Any] = {"type": "websocket.close", "code": code}
        if reason is not None:
            message["reason"] = reason
        await self._send(message)
        self.closed = True

    def _ensure_accepted(self) -> None:
        if not self.accepted or self.closed:
            raise RuntimeError("Accept the WebSocket before sending data")


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
