"""Small ASGI middleware components for lenApi applications."""

import secrets
import time
from collections.abc import Iterable


class RequestIDMiddleware:
    """Attach a safe request ID to ASGI state and the response headers."""

    def __init__(self, app, *, header_name: str = "X-Request-ID") -> None:
        self.app = app
        self.header_name = header_name.encode("latin-1").lower()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        supplied = next(
            (value.decode("latin-1") for name, value in scope.get("headers", [])
             if name.lower() == self.header_name),
            "",
        )
        if not supplied or len(supplied) > 128 or any(
            not (character.isalnum() or character in "._-") for character in supplied
        ):
            supplied = secrets.token_hex(16)
        scope.setdefault("state", {})["request_id"] = supplied

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self.header_name, supplied.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


class TimingMiddleware:
    """Add elapsed request time as an ``X-Process-Time-Ms`` response header."""

    def __init__(self, app, *, header_name: str = "X-Process-Time-Ms") -> None:
        self.app = app
        self.header_name = header_name.encode("latin-1").lower()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - started) * 1000
                headers = list(message.get("headers", []))
                headers.append((self.header_name, f"{elapsed_ms:.3f}".encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_timing)


class CORSMiddleware:
    """Configurable CORS handling for browser clients."""

    def __init__(
        self,
        app,
        *,
        allow_origins: Iterable[str],
        allow_methods: Iterable[str] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"),
        allow_headers: Iterable[str] = ("*",),
        allow_credentials: bool = False,
        expose_headers: Iterable[str] = (),
        max_age: int = 600,
    ) -> None:
        self.app = app
        self.allow_origins = set(allow_origins)
        self.allow_methods = {method.upper() for method in allow_methods}
        self.allow_headers = {header.lower() for header in allow_headers}
        self.allow_credentials = allow_credentials
        self.expose_headers = tuple(expose_headers)
        self.max_age = max_age
        if "*" in self.allow_origins and allow_credentials:
            raise ValueError("CORS wildcard origins cannot be combined with credentials")
        if isinstance(max_age, bool) or max_age < 0:
            raise ValueError("CORS max_age must be a non-negative integer")

    def _origin_headers(self, origin: str) -> list[tuple[bytes, bytes]]:
        allowed = "*" in self.allow_origins or origin in self.allow_origins
        if not allowed:
            return []
        result = [(b"access-control-allow-origin", (origin if self.allow_credentials else
                                                        ("*" if "*" in self.allow_origins else origin)).encode())]
        result.append((b"vary", b"Origin"))
        if self.allow_credentials:
            result.append((b"access-control-allow-credentials", b"true"))
        if self.expose_headers:
            result.append((b"access-control-expose-headers", ", ".join(self.expose_headers).encode()))
        return result

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        origin = headers.get(b"origin", b"").decode("latin-1")
        if not origin:
            return await self.app(scope, receive, send)
        cors_headers = self._origin_headers(origin)
        if not cors_headers:
            return await self.app(scope, receive, send)

        is_preflight = scope.get("method", "GET").upper() == "OPTIONS" and b"access-control-request-method" in headers
        if is_preflight:
            requested_method = headers[b"access-control-request-method"].decode("latin-1").upper()
            requested_headers = [
                name.strip().lower()
                for name in headers.get(b"access-control-request-headers", b"").decode("latin-1").split(",")
                if name.strip()
            ]
            if requested_method not in self.allow_methods or (
                "*" not in self.allow_headers and any(name not in self.allow_headers for name in requested_headers)
            ):
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
            cors_headers.extend([
                (b"access-control-allow-methods", ", ".join(sorted(self.allow_methods)).encode()),
                (b"access-control-max-age", str(self.max_age).encode()),
            ])
            if requested_headers:
                value = ", ".join(requested_headers) if "*" in self.allow_headers else ", ".join(sorted(self.allow_headers))
                cors_headers.append((b"access-control-allow-headers", value.encode()))
            await send({"type": "http.response.start", "status": 204,
                        "headers": cors_headers + [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + cors_headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)
