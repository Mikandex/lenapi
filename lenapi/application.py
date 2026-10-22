"""ASGI application, routers, and route dispatch."""

import inspect
from importlib import import_module
import secrets
import math
import mimetypes
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from .request import Request, reset_current_request, set_current_request
from .response import HTTPException, JSONResponse, Response
from .dependencies import Depends
from .openapi import build_openapi, swagger_html
from .parameters import Body
from .database import AsyncDatabase, Database
from .mongodb import MongoDatabase
from .models import (
    reset_current_async_session,
    reset_current_session,
    set_current_async_session,
    set_current_session,
)
from .security_store import SecurityStoreUnavailable, create_security_store
from .background import BackgroundTasks
from .logging import get_logger
from .service import Cache, WebSocket, WebSocketDisconnect
from .docs_guard import DocsGuard, render_login_page


logger = get_logger("websocket")


class Route:
    def __init__(
        self, path: str, methods: set[str], endpoint: Callable, response_model: Any = None,
        *, api_flag: bool = False, cool_time: int = 300, request_body: dict[str, Any] | None = None,
        include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
    ) -> None:
        if cool_time <= 0:
            raise ValueError("cool_time must be a positive number of seconds")
        if tag_name is not None and (not isinstance(tag_name, str) or not tag_name.strip()):
            raise ValueError("tag_name must be a non-empty string or None")
        if auth is not None and not callable(auth) and not isinstance(auth, Depends):
            raise TypeError("auth must be a callable authentication dependency, Depends instance, or None")
        self.path = path
        self.methods = methods
        self.endpoint = endpoint
        self.response_model = response_model
        self.request_body = request_body
        self.include_in_schema = include_in_schema
        self.tag_name = tag_name
        self.auth = auth
        self.api_flag = api_flag
        self.cool_time = cool_time
        self.regex, self.parameter_names = self._compile_path(path)

    @staticmethod
    def _compile_path(path: str) -> tuple[re.Pattern, list[str]]:
        names: list[str] = []
        pattern = ""
        cursor = 0
        for match in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path):
            pattern += re.escape(path[cursor:match.start()]) + r"([^/]+)"
            names.append(match.group(1))
            cursor = match.end()
        pattern += re.escape(path[cursor:])
        return re.compile("^" + pattern + "$"), names

    def match(self, path: str) -> dict[str, str] | None:
        match = self.regex.match(path)
        if match is None and path != "/" and path.endswith("/"):
            # Route declarations normalize trailing slashes, but accept the
            # trailing-slash form too (for example, OAuth redirect URIs).
            match = self.regex.match(path[:-1])
        return None if match is None else dict(zip(self.parameter_names, match.groups()))


class Router:
    """A group of routes that can be mounted on an Application or another Router."""

    def __init__(self, *, prefix: str = "", tag_name: str | None = None, auth: Any = None) -> None:
        if tag_name is not None and (not isinstance(tag_name, str) or not tag_name.strip()):
            raise ValueError("tag_name must be a non-empty string or None")
        if auth is not None and not callable(auth) and not isinstance(auth, Depends):
            raise TypeError("auth must be a callable authentication dependency, Depends instance, or None")
        self.prefix = self._normalize_prefix(prefix)
        self.tag_name = tag_name
        self.auth = auth
        self.routes: list[Route] = []
        self.websocket_routes: list[Route] = []

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        if not prefix or prefix == "/":
            return ""
        return "/" + prefix.strip("/")

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return "/"
        return "/" + path.strip("/") if path != "/" else "/"

    def route(
        self, path: str, *, methods: tuple[str, ...] = ("GET",), response_model: Any = None,
        api_flag: bool = False, cool_time: int = 300,
        request_body: dict[str, Any] | None = None,
        include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
    ):
        def decorator(endpoint: Callable):
            self.routes.append(Route(
                self.prefix + self._normalize_path(path), {method.upper() for method in methods},
                endpoint, response_model, api_flag=api_flag, cool_time=cool_time,
                request_body=request_body, include_in_schema=include_in_schema,
                tag_name=self.tag_name if tag_name is None else tag_name,
                auth=self.auth if auth is None else auth,
            ))
            return endpoint
        return decorator

    def get(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
            include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None):
        return self.route(path, methods=("GET",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth)

    def post(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
             request_body: dict[str, Any] | None = None, include_in_schema: bool = True,
             tag_name: str | None = None, auth: Any = None):
        return self.route(path, methods=("POST",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, request_body=request_body, include_in_schema=include_in_schema,
                          tag_name=tag_name, auth=auth)

    def put(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
            include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None):
        return self.route(path, methods=("PUT",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth)

    def patch(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
              include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None):
        return self.route(path, methods=("PATCH",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth)

    def delete(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
               include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None):
        return self.route(path, methods=("DELETE",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth)

    def websocket(self, path: str):
        """Register an ASGI WebSocket handler on this router."""
        def decorator(endpoint: Callable):
            self.websocket_routes.append(Route(
                self.prefix + self._normalize_path(path), {"WEBSOCKET"}, endpoint
            ))
            return endpoint
        return decorator

    def include_router(
        self, router: "Router", *, prefix: str = "", tag_name: str | None = None
    ) -> None:
        if tag_name is not None and (not isinstance(tag_name, str) or not tag_name.strip()):
            raise ValueError("tag_name must be a non-empty string or None")
        mount_prefix = self._normalize_prefix(prefix)
        for route in router.routes:
            self.routes.append(Route(
                mount_prefix + route.path, route.methods, route.endpoint, route.response_model,
                api_flag=route.api_flag, cool_time=route.cool_time,
                request_body=route.request_body, include_in_schema=route.include_in_schema,
                tag_name=(tag_name if tag_name is not None else
                          route.tag_name if route.tag_name is not None else self.tag_name),
                auth=route.auth if route.auth is not None else self.auth,
            ))
        for route in router.websocket_routes:
            self.websocket_routes.append(Route(
                mount_prefix + route.path, {"WEBSOCKET"}, route.endpoint
            ))

    def add_router(
        self, prefix: str, router: "Router", *, tag_name: str | None = None
    ) -> None:
        """Mount a router using Django Ninja style: ``app.add_router('/users/', router)``."""
        self.include_router(router, prefix=prefix, tag_name=tag_name)


class Application(Router):
    """An ASGI app with Pydantic validation and optional SQLAlchemy Session injection."""

    def __init__(self, *, session_factory: Callable[[], Any] | None = None, database: Any = None,
                 async_database: AsyncDatabase | None = None,
                 mongodb: MongoDatabase | None = None,
                 title: str = "lenApi", version: str = "0.1.0",
                 docs_url: str | None = "/docs/", openapi_url: str | None = "/openapi.json",
                 max_request_body_size: int | None = 10 * 1024 * 1024,
                 request_body_timeout: float | None = 30.0,
                 ip_flag_backend: str | None = None, redis_url: str | None = None,
                 security_storage: str | None = None,
                 cache_backend: str = "memory",
                 media_root: str | Path | None = None, media_url: str | None = None,
                 tag_name: str | None = None, auth: Any = None,
                 safe_guard_docs_url: bool = False,
                 docs_session_lifetime_seconds: int = 3600,
                 docs_cookie_secure: bool | None = None,
                 docs_login_max_attempts: int = 5,
                 docs_login_window_seconds: int = 300) -> None:
        if session_factory is not None and database is not None:
            raise ValueError("Pass either session_factory or database, not both")
        if max_request_body_size is not None and (
            isinstance(max_request_body_size, bool) or not isinstance(max_request_body_size, int)
            or max_request_body_size < 0
        ):
            raise ValueError("max_request_body_size must be a non-negative integer or None")
        if request_body_timeout is not None and (
            isinstance(request_body_timeout, bool) or not isinstance(request_body_timeout, (int, float))
            or request_body_timeout <= 0
        ):
            raise ValueError("request_body_timeout must be a positive number or None")
        for name, value in (("docs_login_max_attempts", docs_login_max_attempts),
                            ("docs_login_window_seconds", docs_login_window_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        super().__init__(tag_name=tag_name, auth=auth)
        self.database = None if database is False else database if database is not None else (
            None if session_factory is not None or async_database is not None else Database()
        )
        self.session_factory = session_factory or (self.database.session_factory if self.database is not None else None)
        self.async_database = async_database
        self.async_session_factory = async_database.session_factory if async_database is not None else None
        self.mongodb = mongodb
        self.title = title
        self.version = version
        self.docs_url = docs_url
        self.openapi_url = openapi_url
        self.safe_guard_docs_url = bool(safe_guard_docs_url)
        self._docs_guard_paths = {
            normalized
            for configured in (self.docs_url, self.openapi_url)
            if configured
            for normalized in (configured, configured.rstrip("/"))
        }
        self.docs_session_lifetime_seconds = docs_session_lifetime_seconds
        self.docs_cookie_secure = docs_cookie_secure
        self.docs_login_max_attempts = docs_login_max_attempts
        self.docs_login_window_seconds = docs_login_window_seconds
        self._docs_login_attempts: dict[str, tuple[float, int]] = {}
        self._docs_login_lock = threading.Lock()
        if self.safe_guard_docs_url and self.docs_url is None:
            raise ValueError("SAFE_GUARD_DOCS_URL requires DOCS_URL to be enabled")
        self.docs_guard = (
            DocsGuard(
                database=self.database,
                mongodb=self.mongodb,
                session_lifetime_seconds=docs_session_lifetime_seconds,
            )
            if self.database is not None or self.mongodb is not None
            else None
        )
        if self.safe_guard_docs_url and self.docs_guard is None:
            raise ValueError("SAFE_GUARD_DOCS_URL requires a configured SQL or MongoDB database")
        self.media_root = Path(media_root).expanduser().resolve() if media_root is not None else None
        self.media_url = self._normalize_media_url(media_url) if self.media_root is not None and media_url else None
        self.max_request_body_size = max_request_body_size
        self.request_body_timeout = float(request_body_timeout) if request_body_timeout is not None else None
        self._throttle_until: dict[tuple[str, str], float] = {}
        self._throttle_lock = threading.Lock()
        self.user_middleware: list[tuple[type, dict[str, Any]]] = []
        self._middleware_stack = None
        self.security_storage = (security_storage or ip_flag_backend or "memory").lower()
        self.security_store = create_security_store(
            self.security_storage,
            engine=self.database.engine if self.database is not None else None,
            redis_url=redis_url,
            mongodb=self.mongodb,
        )
        self.ip_flag_backend = self.security_storage
        self.cache = Cache(
            cache_backend,
            engine=self.database.engine if self.database is not None else None,
            redis_url=redis_url,
        )

    def add_middleware(self, middleware_class, **options) -> None:
        """Add ASGI middleware, for example ``app.add_middleware(CORSMiddleware, ...)``.

        Middleware is wrapped in the order it is added; the first added is outermost.
        """
        if not callable(middleware_class):
            raise TypeError("middleware_class must be callable")
        self.user_middleware.append((middleware_class, options))
        self._middleware_stack = None

    async def __call__(self, scope, receive, send) -> None:
        if self._middleware_stack is None:
            app = self._dispatch
            for middleware_class, options in reversed(self.user_middleware):
                app = middleware_class(app, **options)
            self._middleware_stack = app
        await self._middleware_stack(scope, receive, send)

    async def _dispatch(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope["type"] == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path, method = scope.get("path", "/"), scope.get("method", "GET").upper()
        route_scope = scope
        docs_login_path = (path == "/" and method in {"GET", "POST"}) or (
            path == "/logout" and method == "POST"
        )
        if self.safe_guard_docs_url and (path in self._docs_guard_paths or docs_login_path):
            guarded_response = await self._dispatch_docs_guard(scope, receive)
            if guarded_response is not None:
                await guarded_response(scope, receive, send)
                return
        if self.docs_url is not None and self.openapi_url is not None and path in {self.docs_url, self.docs_url.rstrip("/")} and method == "GET":
            page = swagger_html(self.openapi_url)
            if self.safe_guard_docs_url:
                signout = '''<form method="post" action="/logout" style="position:fixed;right:18px;top:14px;z-index:9999">
<input type="hidden" name="csrf_token"><button style="border:1px solid #64748b;background:#fff;border-radius:7px;padding:8px 12px;cursor:pointer">Sign out</button>
</form><script>(function(){const c=document.cookie.split('; ').find(x=>x.startsWith('lenapi_docs_csrf='));if(c)document.querySelector('form[action="/logout"] input').value=decodeURIComponent(c.split('=').slice(1).join('='));})();</script>'''
                page = page.replace("</body>", signout + "</body>")
            response = Response(page, media_type="text/html; charset=utf-8")
            await response(route_scope, receive, send)
            return
        if self.openapi_url is not None and path == self.openapi_url and method == "GET":
            response = JSONResponse(build_openapi(self, title=self.title, version=self.version))
            await response(route_scope, receive, send)
            return
        if self.media_url is not None and path.startswith(self.media_url) and method == "GET":
            relative_path = path[len(self.media_url):]
            response = self._media_response(relative_path)
            await response(route_scope, receive, send)
            return
        path_matched = False
        for route in self.routes:
            raw_params = route.match(path)
            if raw_params is None:
                continue
            path_matched = True
            if method not in route.methods:
                continue
            if self.max_request_body_size is not None:
                raw_length = next(
                    (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
                    None,
                )
                if raw_length is not None:
                    try:
                        content_length = int(raw_length)
                    except ValueError:
                        response = JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
                        await response(scope, receive, send)
                        return
                    if content_length < 0:
                        response = JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
                        await response(scope, receive, send)
                        return
                    if content_length > self.max_request_body_size:
                        response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                        await response(scope, receive, send)
                        return
            if route.api_flag:
                client = scope.get("client")
                client_ip = str(client[0]) if client else "unknown"
                try:
                    retry_after = await self._claim_ip_flag(route.path, client_ip, route.cool_time)
                except Exception:
                    hint = (
                        "Check REDIS_URL and Redis availability."
                        if self.ip_flag_backend == "redis"
                        else "Run `python lenora.py migration` to create or update the security store table."
                    )
                    response = JSONResponse(
                        {"detail": f"IP flag storage is unavailable. {hint}"}, status_code=503
                    )
                    await response(scope, receive, send)
                    return
                if retry_after > 0:
                    response = JSONResponse(
                        {"detail": "Too many requests", "cool_time": route.cool_time},
                        status_code=429,
                        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
                    )
                    await response(scope, receive, send)
                    return
            route_scope = {**scope, "path_params": raw_params}
            request = Request(
                route_scope,
                receive,
                max_body_size=self.max_request_body_size,
                body_timeout=self.request_body_timeout,
            )
            request.background_tasks = BackgroundTasks()
            session = None
            async_session = None
            session_token = None
            async_session_token = None
            request_token = set_current_request(request)
            try:
                if self.session_factory is not None:
                    session = self.session_factory()
                    session_token = set_current_session(session)
                if self.async_session_factory is not None:
                    async_session = self.async_session_factory()
                    async_session_token = set_current_async_session(async_session)
                dependency_cache: dict[Callable, Any] = {}
                if route.auth is not None:
                    auth_dependency = route.auth.dependency if isinstance(route.auth, Depends) else route.auth
                    auth_kwargs = await self._build_arguments(
                        auth_dependency, request, raw_params, session, dependency_cache, set(), async_session
                    )
                    identity = auth_dependency(**auth_kwargs)
                    if inspect.isawaitable(identity):
                        identity = await identity
                    dependency_cache[auth_dependency] = identity
                kwargs = await self._build_arguments(
                    route.endpoint, request, raw_params, session, dependency_cache, set(), async_session
                )
                result = route.endpoint(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, Response):
                    response = result
                else:
                    if route.response_model is not None:
                        try:
                            result = TypeAdapter(route.response_model).validate_python(
                                result, from_attributes=True
                            )
                            result = TypeAdapter(route.response_model).dump_python(result, mode="json")
                        except ValidationError as exc:
                            response = JSONResponse({"detail": "Invalid response data"}, status_code=500)
                            await response(route_scope, receive, send)

                            return
                    response = JSONResponse(result)
            except ValidationError:
                response = JSONResponse({"error": "Invalid request"}, status_code=422)
            except MultipleResultsFound:
                response = JSONResponse({"error": "Multiple entries found"}, status_code=409)
            except IntegrityError:
                response = JSONResponse({"error": "Duplicate or conflicting data"}, status_code=409)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
            except SecurityStoreUnavailable:
                hint = (
                    "Run `python lenora.py migration` to create or update the security store table."
                    if self.security_storage == "database"
                    else "Install `lenapi[redis]` and check REDIS_URL and Redis availability."
                )
                response = JSONResponse(
                    {"detail": f"Security storage is unavailable. {hint}"}, status_code=503
                )
            except (TypeError, ValueError, KeyError):
                response = JSONResponse({"error": "Invalid request"}, status_code=422)
            finally:
                reset_current_request(request_token)
                if session_token is not None:
                    reset_current_session(session_token)
                if async_session_token is not None:
                    reset_current_async_session(async_session_token)
                if session is not None:
                    session.close()
                if async_session is not None:
                    await async_session.close()
            await response(route_scope, receive, send)
            if response.status_code < 400:
                await request.background_tasks.run()
            return
        response = JSONResponse({"detail": "Method not allowed" if path_matched else "Not found"},
                                status_code=405 if path_matched else 404)
        await response(scope, receive, send)

    async def _dispatch_docs_guard(self, scope, receive):
        """Serve the private docs login page and gate docs/schema requests."""
        from .request import Request

        path, method = scope.get("path", "/"), scope.get("method", "GET").upper()
        request = Request(scope, receive, max_body_size=4096, body_timeout=self.request_body_timeout)
        secure = (
            self.docs_cookie_secure
            if self.docs_cookie_secure is not None
            else scope.get("scheme") == "https"
        )
        if path in self._docs_guard_paths:
            try:
                identity = await self.docs_guard.resolve_session(
                    request.cookies.get("lenapi_docs_session")
                )
            except RuntimeError:
                identity = None
            if identity is not None:
                return None
            return Response(b"", status_code=303, headers={"location": "/"})

        if path == "/" and method == "GET":
            try:
                identity = await self.docs_guard.resolve_session(
                    request.cookies.get("lenapi_docs_session")
                )
            except RuntimeError:
                identity = None
            if identity is not None:
                return Response(b"", status_code=303, headers={"location": self.docs_url or "/"})
            csrf_token = secrets.token_urlsafe(32)
            try:
                has_admin = await self._has_docs_admin()
            except RuntimeError:
                has_admin = False
            page = render_login_page(
                self.title,
                csrf_token,
                "Create an admin account with `python lenora.py admin` after running migrations."
                if not has_admin
                else None,
            )
            response = Response(page, media_type="text/html; charset=utf-8")
            response.set_cookie(
                "lenapi_docs_csrf", csrf_token, path="/", secure=secure,
                httponly=False, samesite="strict",
            )
            return response

        if path == "/" and method == "POST":
            try:
                form = await request.form()
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            submitted_csrf = form.get("csrf_token", "")
            cookie_csrf = request.cookies.get("lenapi_docs_csrf", "")
            if not isinstance(submitted_csrf, str) or not cookie_csrf or not secrets.compare_digest(
                submitted_csrf, cookie_csrf
            ):
                return JSONResponse({"detail": "Invalid login request; reload the page and try again."}, status_code=403)
            retry_after = self._claim_docs_login(request.client_ip or "unknown")
            if retry_after:
                return JSONResponse(
                    {"detail": "Too many login attempts. Try again later."},
                    status_code=429,
                    headers={"retry-after": str(retry_after)},
                )
            username = form.get("username", "")
            password = form.get("password", "")
            if not isinstance(username, str) or not isinstance(password, str):
                username = password = ""
            try:
                identity = await self.docs_guard.authenticate(username, password)
            except RuntimeError:
                identity = None
            if identity is None:
                csrf_token = secrets.token_urlsafe(32)
                page = render_login_page(self.title, csrf_token, "The username or password is incorrect.")
                response = Response(page, status_code=401, media_type="text/html; charset=utf-8")
                response.set_cookie(
                    "lenapi_docs_csrf", csrf_token, path="/", secure=secure,
                    httponly=False, samesite="strict",
                )
                return response
            self._clear_docs_login_attempts(request.client_ip or "unknown")
            session_id = await self.docs_guard.create_session(identity["id"])
            response = Response(b"", status_code=303, headers={"location": self.docs_url or "/"})
            response.set_cookie(
                "lenapi_docs_session", session_id,
                max_age=self.docs_session_lifetime_seconds,
                path="/", secure=secure, httponly=True, samesite="lax",
            )
            return response

        if path == "/logout" and method == "POST":
            cookie_csrf = request.cookies.get("lenapi_docs_csrf", "")
            try:
                form = await request.form()
            except HTTPException:
                form = {}
            submitted_csrf = form.get("csrf_token", "")
            if not cookie_csrf or not isinstance(submitted_csrf, str) or not secrets.compare_digest(
                submitted_csrf, cookie_csrf
            ):
                return JSONResponse({"detail": "Invalid logout request"}, status_code=403)
            await self.docs_guard.delete_session(request.cookies.get("lenapi_docs_session"))
            response = Response(b"", status_code=303, headers={"location": "/"})
            response.delete_cookie("lenapi_docs_session", path="/")
            response.delete_cookie("lenapi_docs_csrf", path="/")
            return response
        return None

    def _claim_docs_login(self, client_ip: str) -> int:
        """Bound docs-login password guesses per client IP in this worker."""
        now = time.monotonic()
        with self._docs_login_lock:
            if len(self._docs_login_attempts) >= 4096:
                self._docs_login_attempts = {
                    key: value for key, value in self._docs_login_attempts.items()
                    if now - value[0] < self.docs_login_window_seconds
                }
            record = self._docs_login_attempts.get(client_ip)
            if record is not None:
                started, attempts = record
                elapsed = now - started
                if elapsed >= self.docs_login_window_seconds:
                    record = None
                elif attempts >= self.docs_login_max_attempts:
                    return max(1, math.ceil(self.docs_login_window_seconds - elapsed))
            if record is None:
                if len(self._docs_login_attempts) >= 4096 and client_ip not in self._docs_login_attempts:
                    return self.docs_login_window_seconds
                self._docs_login_attempts[client_ip] = (now, 1)
            else:
                self._docs_login_attempts[client_ip] = (record[0], record[1] + 1)
        return 0

    def _clear_docs_login_attempts(self, client_ip: str) -> None:
        with self._docs_login_lock:
            self._docs_login_attempts.pop(client_ip, None)

    async def _has_docs_admin(self) -> bool:
        return await self.docs_guard.has_admin()

    async def _handle_websocket(self, scope, receive, send) -> None:
        initial = await receive()
        if initial.get("type") != "websocket.connect":
            await send({"type": "websocket.close", "code": 1002, "reason": "Expected websocket.connect"})
            return

        path = scope.get("path", "/")
        for route in self.websocket_routes:
            path_params = route.match(path)
            if path_params is None:
                continue
            websocket = WebSocket(scope, receive, send, path_params=path_params)
            try:
                kwargs: dict[str, Any] = {}
                for name, parameter in inspect.signature(route.endpoint).parameters.items():
                    annotation = parameter.annotation
                    if annotation is WebSocket or name in {"websocket", "ws"}:
                        kwargs[name] = websocket
                    elif name in path_params:
                        raw = path_params[name]
                        kwargs[name] = raw if annotation is inspect.Parameter.empty else TypeAdapter(annotation).validate_python(raw)
                    elif parameter.default is inspect.Parameter.empty:
                        raise ValueError(f"Missing WebSocket parameter: {name}")
                result = route.endpoint(**kwargs)
                if inspect.isawaitable(result):
                    await result
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.exception("WebSocket handler failed for %s", path)
                if not websocket.closed:
                    await websocket.close(code=1011, reason="Internal server error")
            else:
                if not websocket.closed:
                    await websocket.close()
            return

        await send({"type": "websocket.close", "code": 4404, "reason": "WebSocket route not found"})

    @staticmethod
    def _normalize_media_url(media_url: str) -> str:
        if not media_url.startswith("/"):
            media_url = "/" + media_url
        media_url = media_url.rstrip("/") + "/"
        if ".." in media_url.split("/"):
            raise ValueError("MEDIA_URL cannot contain '..' path segments")
        return media_url

    def _media_response(self, relative_path: str) -> Response:
        if self.media_root is None or not relative_path:
            return Response(b"Not found", status_code=404)
        root = self.media_root
        file_path = (root / relative_path).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            return Response(b"Not found", status_code=404)
        media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        return Response(file_path.read_bytes(), media_type=media_type)

    async def _handle_lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self.security_storage == "redis":
                    await self.security_store.close()
                await self.cache.close()
                if self.database is not None:
                    self.database.close()
                if self.async_database is not None:
                    await self.async_database.close()
                if self.mongodb is not None:
                    await self.mongodb.aclose()
                await send({"type": "lifespan.shutdown.complete"})

    async def _claim_ip_flag(self, route: str, client_ip: str, cool_time: int) -> int:
        if self.security_storage in {"database", "redis"}:
            return await self.security_store.claim(f"ip_flag:{route}:{client_ip}", cool_time)
        key = (route, client_ip)
        now = time.monotonic()
        with self._throttle_lock:
            if len(self._throttle_until) > 4096:
                self._throttle_until = {
                    item: expiry for item, expiry in self._throttle_until.items() if expiry > now
                }
            retry_after = self._throttle_until.get(key, 0.0) - now
            if retry_after <= 0:
                self._throttle_until[key] = now + cool_time
                return 0
            return math.ceil(retry_after)


    @staticmethod
    def _needs_session(endpoint: Callable) -> bool:
        try:
            from sqlalchemy.orm import Session
        except ImportError:
            return False
        def visit(callable_: Callable, seen: set[Callable]) -> bool:
            if callable_ in seen:
                return False
            seen.add(callable_)
            for parameter in inspect.signature(callable_).parameters.values():
                if parameter.annotation is Session:
                    return True
                if isinstance(parameter.default, Depends) and visit(parameter.default.dependency, seen):
                    return True
            return False
        return visit(endpoint, set())

    @staticmethod
    def _needs_async_session(endpoint: Callable) -> bool:
        def visit(callable_: Callable, seen: set[Callable]) -> bool:
            if callable_ in seen:
                return False
            seen.add(callable_)
            for parameter in inspect.signature(callable_).parameters.values():
                if parameter.annotation is AsyncSession or (
                    isinstance(parameter.annotation, str)
                    and parameter.annotation in {"AsyncSession", "sqlalchemy.ext.asyncio.session.AsyncSession"}
                ):
                    return True
                if isinstance(parameter.default, Depends) and visit(parameter.default.dependency, seen):
                    return True
            return False
        return visit(endpoint, set())

    @classmethod
    async def _build_arguments(cls, endpoint: Callable, request: Request, path_params: dict[str, str], session: Any = None,
                               cache: dict[Callable, Any] | None = None, resolving: set[Callable] | None = None,
                               async_session: AsyncSession | None = None) -> dict[str, Any]:
        cache = {} if cache is None else cache
        resolving = set() if resolving is None else resolving
        values: dict[str, Any] = {}
        query = request.query_params
        body_loaded = False
        body: Any = None
        for name, parameter in inspect.signature(endpoint).parameters.items():
            annotation = parameter.annotation
            if isinstance(parameter.default, Body):
                values[name] = await request.json()
                continue
            if isinstance(parameter.default, Depends):
                dependency = parameter.default.dependency
                if dependency in resolving:
                    raise ValueError(f"Circular dependency detected: {dependency.__name__}")
                if dependency not in cache:
                    resolving.add(dependency)
                    dependency_kwargs = await cls._build_arguments(
                        dependency, request, path_params, session, cache, resolving, async_session
                    )
                    result = dependency(**dependency_kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                    cache[dependency] = result
                    resolving.remove(dependency)
                values[name] = cache[dependency]
                continue
            if name == "request" and (
                annotation in (Request, inspect.Parameter.empty)
                or (isinstance(annotation, str) and annotation.rsplit(".", 1)[-1] == "Request")
            ):
                values[name] = request
                continue
            if annotation is BackgroundTasks:
                values[name] = request.background_tasks
                continue
            if (
                session is not None
                and isinstance(annotation, type)
                and annotation.__module__.startswith("sqlalchemy")
                and annotation.__name__ == "Session"
            ):
                values[name] = session
                continue
            if async_session is not None and (
                annotation is AsyncSession or (
                    isinstance(annotation, str)
                    and annotation in {"AsyncSession", "sqlalchemy.ext.asyncio.session.AsyncSession"}
                )
            ):
                values[name] = async_session
                continue
            if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                if not body_loaded:
                    body = await request.json()
                    body_loaded = True
                values[name] = annotation.model_validate(body)
                continue
            raw = path_params.get(name, query.get(name, inspect.Parameter.empty))
            if raw is inspect.Parameter.empty:
                if parameter.default is not inspect.Parameter.empty:
                    continue
                raise ValueError(f"Missing required parameter: {name}")
            if isinstance(raw, list):
                raise ValueError(f"Parameter '{name}' may only be supplied once")
            values[name] = raw if annotation is inspect.Parameter.empty else TypeAdapter(annotation).validate_python(raw)
        return values


class LENAPI(Application):
    """Application configured from the current project's optional settings.py."""

    def __init__(self, *, session_factory: Callable[[], Any] | None = None, database: Any = None,
                 async_database: AsyncDatabase | None = None,
                 mongodb: MongoDatabase | None = None,
                 title: str | None = None, version: str | None = None,
                 docs_url: str | None = None, openapi_url: str | None = None,
                 max_request_body_size: int | None = None,
                 request_body_timeout: float | None = None,
                 ip_flag_backend: str | None = None, redis_url: str | None = None,
                 security_storage: str | None = None,
                 cache_backend: str | None = None,
                 media_root: str | Path | None = None, media_url: str | None = None,
                 tag_name: str | None = None, auth: Any = None,
                 safe_guard_docs_url: bool | None = None,
                 docs_session_lifetime_seconds: int | None = None,
                 docs_cookie_secure: bool | None = None,
                 docs_login_max_attempts: int | None = None,
                 docs_login_window_seconds: int | None = None) -> None:
        # Project settings are optional, so importing lenapi outside a project
        # continues to work with the framework defaults.
        try:
            project_settings = import_module("settings")
        except ModuleNotFoundError as exc:
            if exc.name != "settings":
                raise
            project_settings = None

        configured_database = database
        configured_async_database = async_database
        configured_mongodb = mongodb
        if project_settings is not None:
            database_type = str(getattr(project_settings, "DATABASE_TYPE", "sqlite")).lower()
            database_url = getattr(project_settings, "DATABASE_URL", None)
            activate_async = bool(getattr(project_settings, "ACTIVATE_ASYNC", True))

            if database_type not in {"sqlite", "mysql", "postgres", "postgresql", "mongodb"}:
                raise ValueError(
                    "DATABASE_TYPE must be 'sqlite', 'mysql', 'postgresql', or 'mongodb'"
                )
            if configured_database is None and configured_mongodb is None and session_factory is None:
                if not database_url:
                    raise ValueError("Set DATABASE_URL in settings.py")
                if database_type == "mongodb":
                    configured_mongodb = MongoDatabase(database_url, async_enabled=activate_async)
                else:
                    sync_url = make_url(database_url)
                    expected = "postgresql" if database_type in {"postgres", "postgresql"} else database_type
                    backend, separator, driver = sync_url.drivername.partition("+")
                    if backend == "postgres":
                        backend = "postgresql"
                    if not separator:
                        driver = {"mysql": "pymysql", "postgresql": "psycopg"}.get(backend, "")
                    sync_url = sync_url.set(
                        drivername=f"{backend}+{driver}" if driver else backend
                    )
                    if backend != expected:
                        raise ValueError(
                            f"DATABASE_URL uses '{backend}', "
                            f"but DATABASE_TYPE is '{database_type}'"
                        )
                    configured_database = Database(sync_url.render_as_string(hide_password=False))
                    if configured_async_database is None and activate_async:
                        async_drivers = {
                            "sqlite": "sqlite+aiosqlite",
                            "mysql": "mysql+asyncmy",
                            "postgresql": "postgresql+asyncpg",
                        }
                        async_url = sync_url.set(
                            drivername=async_drivers[expected]
                        )
                        try:
                            configured_async_database = AsyncDatabase(async_url.render_as_string(hide_password=False))
                        except (ImportError, ModuleNotFoundError) as exc:
                            extras = {
                                "mysql": "async-mysql",
                                "postgresql": "async-postgres",
                            }
                            extra = extras.get(expected)
                            if extra:
                                raise ImportError(
                                    f"Async {expected} access requires `pip install 'lenapi[{extra}]'`"
                                ) from exc
                            raise
            elif configured_mongodb is None and database_type == "mongodb" and database_url:
                configured_mongodb = MongoDatabase(database_url, async_enabled=activate_async)

        # MongoDB is the selected application database; do not also create an
        # implicit SQLite engine through Application's standalone default.
        if configured_mongodb is not None and configured_database is None and session_factory is None:
            configured_database = False

        super().__init__(
            session_factory=session_factory,
            database=configured_database,
            async_database=configured_async_database,
            mongodb=configured_mongodb,
            title=title or getattr(project_settings, "API_TITLE", "lenApi"),
            version=version or getattr(project_settings, "API_VERSION", "0.1.0"),
            docs_url=docs_url if docs_url is not None else getattr(project_settings, "DOCS_URL", "/docs/"),
            openapi_url=openapi_url if openapi_url is not None else getattr(project_settings, "OPENAPI_URL", "/openapi.json"),
            max_request_body_size=(max_request_body_size if max_request_body_size is not None else
                                   getattr(project_settings, "MAX_REQUEST_BODY_SIZE", 10 * 1024 * 1024)),
            request_body_timeout=(request_body_timeout if request_body_timeout is not None else
                                  getattr(project_settings, "REQUEST_BODY_TIMEOUT_SECONDS", 30.0)),
            ip_flag_backend=(ip_flag_backend if ip_flag_backend is not None else
                             getattr(project_settings, "IP_FLAG_BACKEND", None)),
            redis_url=redis_url if redis_url is not None else getattr(
                project_settings, "REDIS_URL", "redis://127.0.0.1:6379/0"
            ),
            security_storage=(security_storage if security_storage is not None else
                              getattr(project_settings, "SECURITY_STORAGE", None)),
            cache_backend=cache_backend or getattr(project_settings, "CACHE_BACKEND", "memory"),
            media_root=media_root if media_root is not None else getattr(project_settings, "MEDIA_ROOT", None),
            media_url=media_url if media_url is not None else getattr(project_settings, "MEDIA_URL", None),
            tag_name=tag_name,
            auth=auth,
            safe_guard_docs_url=(safe_guard_docs_url if safe_guard_docs_url is not None else
                                 getattr(project_settings, "SAFE_GUARD_DOCS_URL", False)),
            docs_session_lifetime_seconds=(docs_session_lifetime_seconds if docs_session_lifetime_seconds is not None else
                                           getattr(project_settings, "DOCS_SESSION_LIFETIME_SECONDS", 3600)),
            docs_cookie_secure=(docs_cookie_secure if docs_cookie_secure is not None else
                                getattr(project_settings, "DOCS_COOKIE_SECURE", None)),
            docs_login_max_attempts=(docs_login_max_attempts if docs_login_max_attempts is not None else
                                     getattr(project_settings, "DOCS_LOGIN_MAX_ATTEMPTS", 5)),
            docs_login_window_seconds=(docs_login_window_seconds if docs_login_window_seconds is not None else
                                       getattr(project_settings, "DOCS_LOGIN_WINDOW_SECONDS", 300)),
        )
        if project_settings is not None:
            from .authentication import install_default_authentication

            install_default_authentication(self, project_settings)
