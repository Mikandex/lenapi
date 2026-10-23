"""Small ASGI middleware components for lenApi applications."""

import secrets
import time
import zlib
from collections.abc import Iterable

from .request import Request
from .response import HTTPException, JSONResponse
from .concurrency import call_sync_or_async
from .models import reset_current_async_session, reset_current_session, set_current_async_session, set_current_session


class GZipMiddleware:
    """Compress eligible HTTP response bodies for clients that accept gzip.

    Buffers only until ``minimum_size`` is reached. Larger responses are then
    compressed as a stream. Compression uses Python's standard library and
    does not add a Starlette dependency.
    """

    _COMPRESSIBLE_TYPES = (
        "text/",
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/xml",
        "application/xhtml+xml",
        "application/atom+xml",
        "application/rss+xml",
        "application/wasm",
        "image/svg+xml",
    )

    def __init__(self, app, *, minimum_size: int = 500, compresslevel: int = 6) -> None:
        if isinstance(minimum_size, bool) or not isinstance(minimum_size, int) or minimum_size < 0:
            raise ValueError("minimum_size must be a non-negative integer")
        if isinstance(compresslevel, bool) or not isinstance(compresslevel, int) or not 0 <= compresslevel <= 9:
            raise ValueError("compresslevel must be an integer from 0 to 9")
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    @staticmethod
    def _accepts_gzip(headers: dict[bytes, bytes]) -> bool:
        encodings = headers.get(b"accept-encoding", b"").decode("latin-1")
        explicit_gzip = None
        wildcard = None
        for item in encodings.split(","):
            pieces = [part.strip() for part in item.split(";")]
            name = pieces[0].lower()
            quality = 1.0
            for parameter in pieces[1:]:
                key, separator, value = parameter.partition("=")
                if separator and key.strip().lower() == "q":
                    try:
                        quality = float(value.strip())
                    except ValueError:
                        quality = 0.0
                    break
            if name == "gzip":
                explicit_gzip = quality
            elif name == "*":
                wildcard = quality
        return (explicit_gzip if explicit_gzip is not None else wildcard or 0.0) > 0

    @classmethod
    def _compressible(cls, headers: dict[bytes, bytes]) -> bool:
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower().decode(
            "latin-1"
        )
        return (
            content_type.startswith("application/")
            and content_type.endswith(("+json", "+xml"))
        ) or any(
            content_type == media_type or media_type.endswith("/") and content_type.startswith(media_type)
            for media_type in cls._COMPRESSIBLE_TYPES
        )

    @staticmethod
    def _append_vary(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        result = []
        vary_values = []
        for name, value in headers:
            if name.lower() == b"vary":
                vary_values.extend(part.strip() for part in value.split(b",") if part.strip())
            else:
                result.append((name, value))
        if not any(value.lower() == b"*" for value in vary_values) and not any(
            value.lower() == b"accept-encoding" for value in vary_values
        ):
            vary_values.append(b"Accept-Encoding")
        if vary_values:
            result.append((b"vary", b", ".join(vary_values)))
        return result

    @staticmethod
    def _gzip_headers(message: dict) -> dict:
        headers = [
            (name, value)
            for name, value in message.get("headers", [])
            if name.lower() not in {b"content-length", b"content-encoding"}
        ]
        headers = GZipMiddleware._append_vary(headers)
        headers.append((b"content-encoding", b"gzip"))
        return {**message, "headers": headers}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_headers = {name.lower(): value for name, value in scope.get("headers", [])}
        accepts_gzip = self._accepts_gzip(request_headers)
        response_start = None
        response_headers = {}
        body_buffer = bytearray()
        compressor = None
        compressing = False
        passthrough = False

        async def send_with_gzip(message):
            nonlocal response_start, response_headers, body_buffer, compressor
            nonlocal compressing, passthrough
            message_type = message["type"]

            if message_type == "http.response.start":
                response_start = message
                response_headers = {
                    name.lower(): value for name, value in message.get("headers", [])
                }
                status = message.get("status", 200)
                cache_control = response_headers.get(b"cache-control", b"").lower()
                passthrough = (
                    not accepts_gzip
                    or not self._compressible(response_headers)
                    or b"content-encoding" in response_headers
                    or b"content-range" in response_headers
                    or b"no-transform" in cache_control
                    or scope.get("method", "GET").upper() == "HEAD"
                    or status in {204, 206, 304}
                )
                if passthrough:
                    headers = list(message.get("headers", []))
                    if self._compressible(response_headers):
                        headers = self._append_vary(headers)
                    await send({**message, "headers": headers})
                return

            if message_type != "http.response.body" or response_start is None:
                await send(message)
                return

            if passthrough:
                await send(message)
                return

            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if not compressing:
                body_buffer.extend(body)
                if len(body_buffer) < self.minimum_size and more_body:
                    return
                if len(body_buffer) < self.minimum_size:
                    headers = list(response_start.get("headers", []))
                    await send({**response_start, "headers": self._append_vary(headers)})
                    await send({**message, "body": bytes(body_buffer)})
                    body_buffer.clear()
                    return

                compressing = True
                compressor = zlib.compressobj(self.compresslevel, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
                await send(self._gzip_headers(response_start))
                compressed = compressor.compress(bytes(body_buffer))
                body_buffer.clear()
                if not more_body:
                    compressed += compressor.flush(zlib.Z_FINISH)
                    compressor = None
                await send({**message, "body": compressed})
                return

            compressed = compressor.compress(body)
            if not more_body:
                compressed += compressor.flush(zlib.Z_FINISH)
                compressor = None
            await send({**message, "body": compressed})

        await self.app(scope, receive, send_with_gzip)


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


class DocsAuthMiddleware:
    """Require an authentication dependency for the documentation endpoints.

    Pass an authenticator from ``lenapi.authentication`` and the docs paths
    configured on the application, for example::

        app.add_middleware(
            DocsAuthMiddleware,
            auth=authentication("basic_login"),
            paths=(app.docs_url, app.openapi_url),
        )
    """

    def __init__(
        self,
        app,
        *,
        auth,
        paths=('/docs/', '/openapi.json'),
        session_factory=None,
        async_session_factory=None,
    ) -> None:
        if auth is None or not callable(auth):
            raise TypeError("DocsAuthMiddleware requires a callable authentication dependency")
        self.app = app
        self.auth = auth.dependency if hasattr(auth, "dependency") else auth
        self.paths = {
            normalized
            for path in paths
            if path
            for normalized in (path, path.rstrip("/"))
        }
        self.session_factory = session_factory
        self.async_session_factory = async_session_factory

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "/") not in self.paths:
            return await self.app(scope, receive, send)

        request = Request(scope, receive, max_body_size=0, body_timeout=1)
        session = self.session_factory() if self.session_factory is not None else None
        async_session = (
            self.async_session_factory() if self.async_session_factory is not None else None
        )
        session_token = set_current_session(session) if session is not None else None
        async_session_token = (
            set_current_async_session(async_session) if async_session is not None else None
        )
        try:
            identity = await call_sync_or_async(self.auth, request)
            if identity is None or identity is False:
                raise HTTPException(401, "Authentication required")
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
            await response(scope, receive, send)
            return
        finally:
            if session_token is not None:
                reset_current_session(session_token)
            if async_session_token is not None:
                reset_current_async_session(async_session_token)
            if session is not None:
                session.close()
            if async_session is not None:
                await async_session.close()

        await self.app(scope, receive, send)


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
