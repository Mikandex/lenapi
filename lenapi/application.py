"""ASGI application, routers, and route dispatch."""

import inspect
from importlib import import_module
import hashlib
import secrets
import math
import mimetypes
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from .request import (
    Request, UploadFile, endpoint_type_hints, reset_current_request, set_current_request,
    upload_file_shape,
)
from .response import FileResponse, HTTPException, JSONResponse, Response
from .dependencies import Depends
from .concurrency import call_sync_or_async
from .openapi import build_openapi, redoc_html, swagger_html
from .parameters import Body, File
from .database import AsyncDatabase, Database
from .mongodb import MongoDatabase
from .models import (
    DatabaseBackendMismatch,
    reset_current_async_session,
    reset_current_database_backend,
    reset_current_session,
    set_current_async_session,
    set_current_database_backend,
    set_current_session,
)
from .security_store import SecurityStoreUnavailable, create_security_store
from .background import BackgroundTasks
from .logging import get_logger
from .service import Cache, WebSocket, WebSocketDisconnect
from .docs_guard import DocsGuard, render_login_page
from .throttling import RateLimit, normalize_throttle
from .sentinels import UNSET


logger = get_logger("websocket")


def _uses_basic_auth(endpoint: Callable, auth: Any) -> bool:
    """Detect BasicAuth supplied directly or through endpoint dependencies."""
    from .security import BasicAuth

    seen: set[int] = set()

    def visit(dependency: Any) -> bool:
        if isinstance(dependency, Depends):
            dependency = dependency.dependency
        if isinstance(dependency, BasicAuth):
            return True
        if not callable(dependency) or id(dependency) in seen:
            return False
        seen.add(id(dependency))
        try:
            parameters = inspect.signature(dependency).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            isinstance(parameter.default, Depends) and visit(parameter.default)
            for parameter in parameters
        )

    return visit(auth) or visit(endpoint)


class Route:
    def __init__(
        self, path: str, methods: set[str], endpoint: Callable, response_model: Any = None,
        *, api_flag: bool = False, cool_time: int = 300, request_body: dict[str, Any] | None = None,
        include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
        throttle: tuple[RateLimit, ...] | None = None,
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
        self.throttle = throttle
        if _uses_basic_auth(endpoint, auth) and throttle != () and not any(
            limit.scope in {"ip", "anonymous"} for limit in (throttle or ())
        ):
            # BasicAuth may perform an expensive password hash verification.
            # Provide a pre-auth IP limit unless the route explicitly opts out
            # with throttle={}; preserve any authenticated quota supplied.
            self.throttle = (throttle or ()) + (RateLimit("ip", 10, 60),)
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

    def __init__(self, *, prefix: str = "", tag_name: str | None = None, auth: Any = None,
                 throttle: dict[str, str] | None = None) -> None:
        if tag_name is not None and (not isinstance(tag_name, str) or not tag_name.strip()):
            raise ValueError("tag_name must be a non-empty string or None")
        if auth is not None and not callable(auth) and not isinstance(auth, Depends):
            raise TypeError("auth must be a callable authentication dependency, Depends instance, or None")
        self.prefix = self._normalize_prefix(prefix)
        self.tag_name = tag_name
        self.auth = auth
        self.throttle = normalize_throttle(throttle)
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
        throttle: dict[str, str] | None = None,
    ):
        def decorator(endpoint: Callable):
            self.routes.append(Route(
                self.prefix + self._normalize_path(path), {method.upper() for method in methods},
                endpoint, response_model, api_flag=api_flag, cool_time=cool_time,
                request_body=request_body, include_in_schema=include_in_schema,
                tag_name=self.tag_name if tag_name is None else tag_name,
                auth=self.auth if auth is None else auth,
                throttle=self.throttle if throttle is None else normalize_throttle(throttle),
            ))
            return endpoint
        return decorator

    def get(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
            include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
            throttle: dict[str, str] | None = None):
        return self.route(path, methods=("GET",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth, throttle=throttle)

    def post(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
             request_body: dict[str, Any] | None = None, include_in_schema: bool = True,
             tag_name: str | None = None, auth: Any = None, throttle: dict[str, str] | None = None):
        return self.route(path, methods=("POST",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, request_body=request_body, include_in_schema=include_in_schema,
                          tag_name=tag_name, auth=auth, throttle=throttle)

    def put(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
            include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
            throttle: dict[str, str] | None = None):
        return self.route(path, methods=("PUT",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth, throttle=throttle)

    def patch(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
              include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
              throttle: dict[str, str] | None = None):
        return self.route(path, methods=("PATCH",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth, throttle=throttle)

    def delete(self, path: str, *, response_model: Any = None, api_flag: bool = False, cool_time: int = 300,
               include_in_schema: bool = True, tag_name: str | None = None, auth: Any = None,
               throttle: dict[str, str] | None = None):
        return self.route(path, methods=("DELETE",), response_model=response_model, api_flag=api_flag,
                          cool_time=cool_time, include_in_schema=include_in_schema, tag_name=tag_name,
                          auth=auth, throttle=throttle)

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
                throttle=route.throttle if route.throttle is not None else self.throttle,
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
                 database_type: str | None = None,
                 title: str = "lenApi", version: str = "0.1.0",
                 debug: bool = False,
                 docs_url: str | None = "/docs/", redoc_url: str | None = "/redoc/",
                 openapi_url: str | None = "/openapi.json",
                 max_request_body_size: int | None = 10 * 1024 * 1024,
                 request_body_timeout: float | None = 30.0,
                 ip_flag_backend: str | None = None, redis_url: str | None = None,
                 security_storage: str | None = None,
                 cache_backend: str = "memory",
                 media_root: str | Path | None = None, media_url: str | None = None,
                 private_media_root: str | Path | None = None,
                 tag_name: str | None = None, auth: Any = None,
                 throttle: dict[str, str] | None = None,
                 safe_guard_docs_url: bool = False,
                 docs_session_lifetime_seconds: int = 3600,
                 docs_cookie_secure: bool | None = None,
                 docs_login_max_attempts: int = 5,
                 docs_login_window_seconds: int = 300,
                 admin_enabled: bool = False, admin_url: str = "/admin/",
                 admin_session_lifetime_seconds: int = 3600,
                 admin_cookie_secure: bool | None = None,
                 admin_monitoring_enabled: bool = False,
                 project_namespace: str | None = None) -> None:
        if session_factory is not None and database is not None:
            raise ValueError("Pass either session_factory or database, not both")
        if not isinstance(debug, bool):
            raise ValueError("debug must be a boolean")
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
        for name, value in (("admin_session_lifetime_seconds", admin_session_lifetime_seconds),):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(admin_enabled, bool) or not isinstance(admin_monitoring_enabled, bool):
            raise ValueError("ADMIN_ENABLED and ADMIN_MONITORING_ENABLED must be booleans")
        if not isinstance(admin_url, str) or not admin_url.startswith("/") or "?" in admin_url or "#" in admin_url:
            raise ValueError("ADMIN_URL must be an absolute path beginning with '/'")
        if admin_monitoring_enabled and not admin_enabled:
            raise ValueError("ADMIN_MONITORING_ENABLED requires ADMIN_ENABLED = True")
        super().__init__(tag_name=tag_name, auth=auth, throttle=throttle)
        self.database = None if database is False else database if database is not None else (
            None if session_factory is not None or async_database is not None else Database()
        )
        self.session_factory = session_factory or (self.database.session_factory if self.database is not None else None)
        self.async_database = async_database
        self.async_session_factory = async_database.session_factory if async_database is not None else None
        self.mongodb = mongodb
        # Make the selected backend visible to model helpers during requests,
        # so a SQL-model call in MongoDB mode can explain the mismatch clearly.
        inferred_backend = "mongodb" if mongodb is not None else (
            "sql" if self.database is not None or self.session_factory is not None else None
        )
        self.database_type = database_type or inferred_backend
        self.title = title
        self.version = version
        self.debug = debug
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.safe_guard_docs_url = bool(safe_guard_docs_url)
        self._docs_guard_paths = {
            normalized
            for configured in (self.docs_url, self.redoc_url, self.openapi_url)
            if configured
            for normalized in (configured, configured.rstrip("/"))
        }
        self.docs_session_lifetime_seconds = docs_session_lifetime_seconds
        self.docs_cookie_secure = docs_cookie_secure
        self.docs_login_max_attempts = docs_login_max_attempts
        self.docs_login_window_seconds = docs_login_window_seconds
        self._docs_login_attempts: dict[str, tuple[float, int]] = {}
        self._docs_login_lock = threading.Lock()
        self.project_namespace = project_namespace
        if self.safe_guard_docs_url and self.docs_url is None:
            raise ValueError("SAFE_GUARD_DOCS_URL requires DOCS_URL to be enabled")
        self.docs_guard = (
            DocsGuard(
                database=self.database,
                mongodb=self.mongodb,
                session_lifetime_seconds=docs_session_lifetime_seconds,
                project_namespace=project_namespace,
            )
            if self.database is not None or self.mongodb is not None
            else None
        )
        if self.safe_guard_docs_url and self.docs_guard is None:
            raise ValueError("SAFE_GUARD_DOCS_URL requires a configured SQL or MongoDB database")
        if admin_enabled and (self.docs_guard is None or (self.database is None and self.mongodb is None)):
            raise ValueError("ADMIN_ENABLED requires a configured SQL or MongoDB database")
        self.admin_enabled = admin_enabled
        self.admin_url = admin_url
        self.admin_session_lifetime_seconds = admin_session_lifetime_seconds
        self.admin_monitoring_enabled = admin_monitoring_enabled
        from .admin import AdminConsole
        self.admin_console = AdminConsole(
            self, path=admin_url, monitor=admin_monitoring_enabled,
            cookie_secure=admin_cookie_secure,
        )
        self.media_root = Path(media_root).expanduser().resolve() if media_root is not None else None
        self.media_url = self._normalize_media_url(media_url) if self.media_root is not None and media_url else None
        self.private_media_root = (
            Path(private_media_root).expanduser().resolve()
            if private_media_root is not None else None
        )
        self.max_request_body_size = max_request_body_size
        self.request_body_timeout = float(request_body_timeout) if request_body_timeout is not None else None
        self._throttle_until: dict[tuple[str, str], float] = {}
        self._rate_limit_counters: dict[str, tuple[float, int]] = {}
        self._throttle_lock = threading.Lock()
        self.user_middleware: list[tuple[type, dict[str, Any]]] = []
        self._middleware_stack = None
        self._mounted_apps: dict[str, Application] = {}
        self._admin_parent: Application | None = None
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

    def mount(self, path: str, app: "Application | str") -> None:
        """Mount an independent LenAPI app at any chosen URL prefix.

        Pass an application instance, or the name of a sibling generated
        project (for example ``app.mount("/gamer", "games")``).
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("mount path must be an absolute path such as '/gamer'")
        normalized = "/" + path.strip("/")
        if normalized == "/" or "?" in normalized or "#" in normalized:
            raise ValueError("mount path must be a named absolute path such as '/gamer'")
        if isinstance(app, str):
            if not app.isidentifier() or app.lower() == "lenora":
                raise ValueError("mounted project must be a valid project name")
            project_dir = getattr(self, "project_dir", None)
            if project_dir is None:
                raise ValueError(
                    "mounting a project name requires an app created by the generated "
                    "LenAPI project bootstrap"
                )
            from lenapi_bootstrap import load_project_app

            child_dir = project_dir.parent / app
            if not child_dir.is_dir():
                raise ValueError(f"mounted project directory does not exist: {child_dir}")
            app = load_project_app(child_dir)
        if not isinstance(app, Application):
            raise TypeError("mount expects another LENAPI application or a sibling project name")
        project_name = getattr(app, "project_name", None)
        if not project_name:
            raise ValueError("mounted application must define PROJECT_NAME in its settings")
        if normalized in self._mounted_apps:
            raise ValueError(f"an application is already mounted at {normalized!r}")
        if app is self:
            raise ValueError("an application cannot mount itself")
        if app._admin_parent is not None and app._admin_parent is not self:
            raise ValueError("a mounted application can have only one parent admin")
        app._admin_parent = self
        self._mounted_apps[normalized] = app

    async def _dispatch_mounted_app(self, scope, receive, send) -> bool:
        if scope.get("type") not in {"http", "websocket"}:
            return False
        path = scope.get("path", "/")
        for prefix, mounted_app in sorted(
            self._mounted_apps.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if path != prefix and not path.startswith(prefix + "/"):
                continue
            child_scope = dict(scope)
            child_scope["root_path"] = scope.get("root_path", "").rstrip("/") + prefix
            child_scope["path"] = path[len(prefix):] or "/"
            raw_path = scope.get("raw_path")
            if isinstance(raw_path, bytes):
                raw_prefix = prefix.encode("utf-8")
                if raw_path.startswith(raw_prefix):
                    child_scope["raw_path"] = raw_path[len(raw_prefix):] or b"/"
            await mounted_app(child_scope, receive, send)
            return True
        return False

    async def _dispatch(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if await self._dispatch_mounted_app(scope, receive, send):
            return
        if scope["type"] == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path, method = scope.get("path", "/"), scope.get("method", "GET").upper()
        root_path = scope.get("root_path", "").rstrip("/")
        external_openapi_url = f"{root_path}{self.openapi_url}" if root_path and self.openapi_url else self.openapi_url
        route_scope = scope
        try:
            admin_response = await self.admin_console.dispatch(scope, receive)
        except Exception:
            if self.debug:
                logger.exception("Admin console request failed for %s", path)
            else:
                logger.error("Admin console request failed for %s", path)
            admin_response = JSONResponse({"detail": "Admin service is temporarily unavailable"}, status_code=503)
        if admin_response is not None:
            await admin_response(route_scope, receive, send)
            return
        docs_login_path = (path == "/" and method in {"GET", "POST"}) or (
            path == "/logout" and method == "POST"
        )
        if self.safe_guard_docs_url and (path in self._docs_guard_paths or docs_login_path):
            guarded_response = await self._dispatch_docs_guard(scope, receive)
            if guarded_response is not None:
                await guarded_response(scope, receive, send)
                return
        if self.docs_url is not None and self.openapi_url is not None and path in {self.docs_url, self.docs_url.rstrip("/")} and method == "GET":
            page = swagger_html(external_openapi_url)
            if self.safe_guard_docs_url:
                signout_path = f"{root_path}/logout" or "/logout"
                signout = f'''<form method="post" action="{signout_path}" style="position:fixed;right:18px;top:14px;z-index:9999">
<input type="hidden" name="csrf_token"><button style="border:1px solid #64748b;background:#fff;border-radius:7px;padding:8px 12px;cursor:pointer">Sign out</button>
</form><script>(function(){{const c=document.cookie.split('; ').find(x=>x.startsWith('lenapi_docs_csrf='));if(c)document.querySelector('form[action="{signout_path}"] input').value=decodeURIComponent(c.split('=').slice(1).join('='));}})();</script>'''
                page = page.replace("</body>", signout + "</body>")
            response = Response(page, media_type="text/html; charset=utf-8")
            await response(route_scope, receive, send)
            return
        if self.redoc_url is not None and self.openapi_url is not None and path in {
            self.redoc_url, self.redoc_url.rstrip("/")
        } and method == "GET":
            page = redoc_html(external_openapi_url)
            if self.safe_guard_docs_url:
                signout_path = f"{root_path}/logout" or "/logout"
                signout = f'''<form method="post" action="{signout_path}" style="position:fixed;right:18px;top:14px;z-index:9999">
<input type="hidden" name="csrf_token"><button style="border:1px solid #64748b;background:#fff;border-radius:7px;padding:8px 12px;cursor:pointer">Sign out</button>
</form><script>(function(){{const c=document.cookie.split('; ').find(x=>x.startsWith('lenapi_docs_csrf='));if(c)document.querySelector('form[action="{signout_path}"] input').value=decodeURIComponent(c.split('=').slice(1).join('='));}})();</script>'''
                page = page.replace("</body>", signout + "</body>")
            response = Response(page, media_type="text/html; charset=utf-8")
            await response(route_scope, receive, send)
            return
        if self.openapi_url is not None and path == self.openapi_url and method == "GET":
            schema = build_openapi(self, title=self.title, version=self.version)
            if root_path:
                schema["servers"] = [{"url": root_path}]
            response = JSONResponse(schema)
            await response(route_scope, receive, send)
            return
        if self.media_url is not None and method == "GET":
            if self.media_url == "/":
                # A root media URL is useful for generated names such as
                # /<uuid>.jpg. Only serve a file if it exists; otherwise let
                # ordinary application routes (including /) continue routing.
                relative_path = path.lstrip("/")
                if relative_path and "/" not in relative_path:
                    response = self._media_response(relative_path)
                    if response.status_code != 404:
                        await response(route_scope, receive, send)
                        return
            elif path.startswith(self.media_url):
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
                media_root=self.media_root,
                private_media_root=self.private_media_root,
            )
            request.background_tasks = BackgroundTasks()
            session = None
            async_session = None
            session_token = None
            async_session_token = None
            database_backend_token = set_current_database_backend(self.database_type)
            request_token = set_current_request(request)
            try:
                if self.session_factory is not None:
                    session = self.session_factory()
                    session_token = set_current_session(session)
                if self.async_session_factory is not None:
                    async_session = self.async_session_factory()
                    async_session_token = set_current_async_session(async_session)
                dependency_cache: dict[Callable, Any] = {}
                response = None
                # Authentication credentials are untrusted until verified.
                # Apply anonymous limits before calling the authenticator so
                # password hashing and other expensive checks are rate-limited.
                pre_auth_scopes = {"ip", "anonymous"}
                response = await self._enforce_rate_limits(
                    route, request, None, scopes=pre_auth_scopes
                )
                if response is None and route.auth is not None:
                    auth_dependency = route.auth.dependency if isinstance(route.auth, Depends) else route.auth
                    auth_kwargs = await self._build_arguments(
                        auth_dependency, request, raw_params, session, dependency_cache, set(), async_session
                    )
                    identity = await call_sync_or_async(auth_dependency, **auth_kwargs)
                    dependency_cache[auth_dependency] = identity
                    response = await self._enforce_rate_limits(
                        route, request, identity, scopes={"authenticated"}
                    )
                if response is None:
                    kwargs = await self._build_arguments(
                        route.endpoint, request, raw_params, session, dependency_cache, set(), async_session
                    )
                    result = await call_sync_or_async(route.endpoint, **kwargs)
                    if isinstance(result, Response):
                        response = result
                    else:
                        if route.response_model is not None:
                            try:
                                result = TypeAdapter(route.response_model).validate_python(
                                    result, from_attributes=True
                                )
                                result = TypeAdapter(route.response_model).dump_python(result, mode="json")
                            except ValidationError:
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
            except DatabaseBackendMismatch as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=500)
            except Exception:
                if self.debug:
                    logger.exception("Unhandled application error for %s %s", method, path)
                else:
                    logger.error("Unhandled application error for %s %s", method, path)
                response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
            finally:
                reset_current_request(request_token)
                reset_current_database_backend(database_backend_token)
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
        root_path = scope.get("root_path", "").rstrip("/")
        base_path = f"{root_path}/" if root_path else "/"
        docs_path = f"{root_path}{self.docs_url}" if root_path and self.docs_url else (self.docs_url or "/")
        cookie_path = root_path or "/"
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
            return Response(b"", status_code=303, headers={"location": base_path})

        if path == "/" and method == "GET":
            try:
                identity = await self.docs_guard.resolve_session(
                    request.cookies.get("lenapi_docs_session")
                )
            except RuntimeError:
                identity = None
            if identity is not None:
                return Response(b"", status_code=303, headers={"location": docs_path})
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
            if root_path:
                page = page.replace('action="/"', f'action="{base_path}"')
            response = Response(page, media_type="text/html; charset=utf-8")
            response.set_cookie(
                "lenapi_docs_csrf", csrf_token, path=cookie_path, secure=secure,
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
            client_ip = request.client_ip or "unknown"
            try:
                retry_after = await self._claim_docs_login_shared(client_ip)
            except SecurityStoreUnavailable:
                return JSONResponse(
                    {"detail": "Documentation login rate-limit storage is unavailable."},
                    status_code=503,
                )
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
                if root_path:
                    page = page.replace('action="/"', f'action="{base_path}"')
                response = Response(page, status_code=401, media_type="text/html; charset=utf-8")
                response.set_cookie(
                    "lenapi_docs_csrf", csrf_token, path=cookie_path, secure=secure,
                    httponly=False, samesite="strict",
                )
                return response
            try:
                await self._clear_docs_login_attempts_shared(client_ip)
            except SecurityStoreUnavailable:
                return JSONResponse(
                    {"detail": "Documentation login rate-limit storage is unavailable."},
                    status_code=503,
                )
            session_id = await self.docs_guard.create_session(identity["id"])
            response = Response(b"", status_code=303, headers={"location": docs_path})
            response.set_cookie(
                "lenapi_docs_session", session_id,
                max_age=self.docs_session_lifetime_seconds,
                path=cookie_path, secure=secure, httponly=True, samesite="lax",
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
            response = Response(b"", status_code=303, headers={"location": base_path})
            response.delete_cookie("lenapi_docs_session", path=cookie_path)
            response.delete_cookie("lenapi_docs_csrf", path=cookie_path)
            return response
        return None

    def _claim_docs_login(self, client_ip: str) -> int:
        """Bound docs-login guesses per client IP in this worker (memory mode)."""
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

    async def _claim_docs_login_shared(self, client_ip: str) -> int:
        """Count docs-login attempts in the configured shared security store."""
        if self.security_store is None:
            return self._claim_docs_login(client_ip)
        key = f"docs_login_attempts:{self.docs_url or '/'}:{client_ip}"
        count, retry_after = await self.security_store.increment(
            key, self.docs_login_window_seconds
        )
        return retry_after if count > self.docs_login_max_attempts else 0

    def _clear_docs_login_attempts(self, client_ip: str) -> None:
        with self._docs_login_lock:
            self._docs_login_attempts.pop(client_ip, None)

    async def _clear_docs_login_attempts_shared(self, client_ip: str) -> None:
        """Clear docs-login counters after successful authentication."""
        if self.security_store is None:
            self._clear_docs_login_attempts(client_ip)
            return
        key = f"docs_login_attempts:{self.docs_url or '/'}:{client_ip}"
        await self.security_store.delete(key)

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
                await call_sync_or_async(route.endpoint, **kwargs)
            except WebSocketDisconnect:
                pass
            except Exception:
                if self.debug:
                    logger.exception("WebSocket handler failed for %s", path)
                else:
                    logger.error("WebSocket handler failed for %s", path)
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
        # Only passive raster formats are served inline. Other files from a
        # public upload directory download instead of executing in the app's
        # origin (for example HTML, SVG, XML, or script files).
        inline_image_types = {
            "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
        }
        headers = {"X-Content-Type-Options": "nosniff"}
        if media_type.lower() not in inline_image_types:
            headers["Content-Disposition"] = "attachment"
        return FileResponse(file_path, media_type=media_type, headers=headers)

    def private_file(self, relative_path: str, *, inline: bool = False) -> Response:
        """Return a private file response for use inside an authenticated route.

        This helper does not authenticate or authorize the caller. Protect the
        route with ``auth=...`` and check that the caller may access this exact
        record before returning the response.
        """
        if not isinstance(inline, bool):
            raise TypeError("inline must be a boolean")
        if self.private_media_root is None or not isinstance(relative_path, str) or not relative_path:
            return Response(b"Not found", status_code=404)
        relative = Path(relative_path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            return Response(b"Not found", status_code=404)
        root = self.private_media_root
        file_path = (root / relative).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            return Response(b"Not found", status_code=404)
        media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        inline_image_types = {
            "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
        }
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if not inline or media_type.lower() not in inline_image_types:
            headers["Content-Disposition"] = "attachment"
        return FileResponse(file_path, media_type=media_type, headers=headers)

    async def _handle_lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                visited = set()

                async def close_tree(application):
                    if id(application) in visited:
                        return
                    visited.add(id(application))
                    if application.security_storage == "redis":
                        await application.security_store.close()
                    await application.cache.close()
                    if application.database is not None:
                        application.database.close()
                    if application.async_database is not None:
                        await application.async_database.close()
                    if application.mongodb is not None:
                        await application.mongodb.aclose()
                    for child in application._mounted_apps.values():
                        await close_tree(child)

                await close_tree(self)
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
    def _authenticated_principal(identity: Any) -> str:
        """Get a stable principal identifier from an already verified identity."""
        if isinstance(identity, dict):
            for name in ("sub", "user_id", "id", "pk", "username", "email"):
                value = identity.get(name)
                if value is not None and str(value):
                    return f"{name}:{value}"
            if identity.get("auth_method"):
                return f"auth_method:{identity['auth_method']}"
            return "authenticated:shared"
        if isinstance(identity, (str, int)) and not isinstance(identity, bool):
            return f"identity:{identity}"
        for name in ("sub", "user_id", "id", "pk", "username", "email"):
            value = getattr(identity, name, None)
            if value is not None and str(value):
                return f"{name}:{value}"
        return f"type:{type(identity).__module__}.{type(identity).__qualname__}"

    async def _claim_rate_limit(self, key: str, window_seconds: int) -> tuple[int, int]:
        if self.security_store is not None:
            return await self.security_store.increment(key, window_seconds)
        now = time.monotonic()
        with self._throttle_lock:
            if len(self._rate_limit_counters) >= 8192:
                self._rate_limit_counters = {
                    item: record for item, record in self._rate_limit_counters.items()
                    if record[0] > now
                }
            record = self._rate_limit_counters.get(key)
            if record is None or record[0] <= now:
                if len(self._rate_limit_counters) >= 8192 and key not in self._rate_limit_counters:
                    return 1, window_seconds
                self._rate_limit_counters[key] = (now + window_seconds, 1)
                return 1, window_seconds
            expires_at, count = record
            count += 1
            self._rate_limit_counters[key] = (expires_at, count)
            return count, max(1, math.ceil(expires_at - now))

    async def _enforce_rate_limits(
        self, route: Route, request: Request, identity: Any, *, scopes: set[str]
    ) -> Response | None:
        for limit in route.throttle or ():
            if limit.scope not in scopes:
                continue
            if limit.scope == "authenticated":
                if identity is None or identity is False:
                    continue
                principal = self._authenticated_principal(identity)
            else:
                if limit.scope == "anonymous" and identity is not None:
                    continue
                principal = request.client_ip or "unknown"
            raw_key = f"rate_limit:{request.method.upper()}:{route.path}:{limit.scope}:{principal}"
            key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            count, retry_after = await self._claim_rate_limit(key, limit.window_seconds)
            if count > limit.count:
                return JSONResponse(
                    {"detail": "Too many requests"},
                    status_code=429,
                    headers={
                        "Retry-After": str(max(1, retry_after)),
                        "X-RateLimit-Limit": str(limit.count),
                        "X-RateLimit-Remaining": "0",
                    },
                )
        return None


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
        form_loaded = False
        form: dict[str, Any] = {}
        type_hints = endpoint_type_hints(endpoint)
        for name, parameter in inspect.signature(endpoint).parameters.items():
            annotation = type_hints.get(name, parameter.annotation)
            file_shape = upload_file_shape(annotation)
            if file_shape is not None:
                file_marker = parameter.default if isinstance(parameter.default, File) else None
                file_default = file_marker.default if file_marker is not None else parameter.default
                file_required = file_default is inspect.Parameter.empty or file_default is ...
                if not form_loaded:
                    form = await request.form()
                    form_loaded = True
                uploaded = form.get(name)
                if uploaded is None:
                    if not file_required:
                        values[name] = file_default
                        continue
                    raise HTTPException(422, f"Missing uploaded file: {name}")
                if file_shape == "multiple":
                    items = uploaded if isinstance(uploaded, list) else [uploaded]
                    if not all(isinstance(item, UploadFile) for item in items):
                        raise HTTPException(422, f"'{name}' must contain uploaded files")
                    values[name] = items
                else:
                    if isinstance(uploaded, list):
                        if len(uploaded) != 1:
                            raise HTTPException(422, f"'{name}' may only contain one uploaded file")
                        uploaded = uploaded[0]
                    if not isinstance(uploaded, UploadFile):
                        raise HTTPException(422, f"'{name}' must be an uploaded file")
                    values[name] = uploaded
                continue
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
                    result = await call_sync_or_async(dependency, **dependency_kwargs)
                    cache[dependency] = result
                    resolving.remove(dependency)
                values[name] = cache[dependency]
                continue
            if name == "request" and (
                annotation in (Request, inspect.Parameter.empty)
                or Request in get_args(annotation)
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
                raise HTTPException(422, f"Missing required parameter: {name}")
            if isinstance(raw, list):
                raise HTTPException(422, f"Parameter '{name}' may only be supplied once")
            values[name] = raw if annotation is inspect.Parameter.empty else TypeAdapter(annotation).validate_python(raw)
        return values


class LENAPI(Application):
    """Application configured from the current project's optional settings.py."""

    def __init__(self, *, settings: Any = None,
                 session_factory: Callable[[], Any] | None = None, database: Any = None,
                 async_database: AsyncDatabase | None = None,
                 mongodb: MongoDatabase | None = None,
                 title: str | None = None, version: str | None = None,
                 debug: bool | None = None,
                 docs_url: Any = UNSET, redoc_url: Any = UNSET,
                 openapi_url: Any = UNSET,
                 max_request_body_size: Any = UNSET,
                 request_body_timeout: Any = UNSET,
                 ip_flag_backend: str | None = None, redis_url: str | None = None,
                 security_storage: str | None = None,
                 cache_backend: str | None = None,
                 media_root: Any = UNSET, media_url: Any = UNSET,
                 tag_name: str | None = None, auth: Any = None,
                 throttle: dict[str, str] | None = None,
                 safe_guard_docs_url: bool | None = None,
                 docs_session_lifetime_seconds: int | None = None,
                 docs_cookie_secure: Any = UNSET,
                 docs_login_max_attempts: int | None = None,
                 docs_login_window_seconds: int | None = None,
                 private_media_root: Any = UNSET,
                 admin_enabled: bool | None = None, admin_url: str | None = None,
                 admin_session_lifetime_seconds: int | None = None,
                 admin_cookie_secure: Any = UNSET,
                 admin_monitoring_enabled: bool | None = None) -> None:
        # Project settings are optional, so importing lenapi outside a project
        # continues to work with the framework defaults.
        if settings is not None:
            project_settings = settings
        else:
            try:
                project_settings = import_module("settings")
            except ModuleNotFoundError as exc:
                if exc.name != "settings":
                    raise
                project_settings = None

        if project_settings is not None:
            project_dir = getattr(project_settings, "BASE_DIR", None)
            if project_dir is not None and getattr(project_settings, "PROJECT_NAME", None):
                from .project import configure_project_from_settings

                configure_project_from_settings(project_settings, project_dir)

        configured_database = database
        configured_async_database = async_database
        configured_mongodb = mongodb
        database_type = None
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
                    configured_mongodb = MongoDatabase(
                        database_url,
                        database_name=getattr(project_settings, "MONGODB_DATABASE", None),
                        async_enabled=activate_async,
                    )
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
            database_type = "mongodb"
        elif configured_database is not None and database_type is None:
            database_type = "sql"

        super().__init__(
            session_factory=session_factory,
            database=configured_database,
            async_database=configured_async_database,
            mongodb=configured_mongodb,
            database_type=("postgresql" if database_type == "postgres" else database_type),
            title=title or getattr(project_settings, "API_TITLE", "lenApi"),
            version=version or getattr(project_settings, "API_VERSION", "0.1.0"),
            debug=(debug if debug is not None else
                   getattr(project_settings, "DEBUG", False)),
            docs_url=_setting_override(docs_url, project_settings, "DOCS_URL", "/docs/"),
            redoc_url=_setting_override(redoc_url, project_settings, "REDOC_URL", "/redoc/"),
            openapi_url=_setting_override(openapi_url, project_settings, "OPENAPI_URL", "/openapi.json"),
            max_request_body_size=_setting_override(
                max_request_body_size, project_settings, "MAX_REQUEST_BODY_SIZE", 10 * 1024 * 1024
            ),
            request_body_timeout=_setting_override(
                request_body_timeout, project_settings, "REQUEST_BODY_TIMEOUT_SECONDS", 30.0
            ),
            ip_flag_backend=(ip_flag_backend if ip_flag_backend is not None else
                             getattr(project_settings, "IP_FLAG_BACKEND", None)),
            redis_url=redis_url if redis_url is not None else getattr(
                project_settings, "REDIS_URL", "redis://127.0.0.1:6379/0"
            ),
            security_storage=(security_storage if security_storage is not None else
                              getattr(project_settings, "SECURITY_STORAGE", None)),
            cache_backend=cache_backend or getattr(project_settings, "CACHE_BACKEND", "memory"),
            media_root=_setting_override(media_root, project_settings, "MEDIA_ROOT", None),
            media_url=_setting_override(media_url, project_settings, "MEDIA_URL", None),
            private_media_root=_setting_override(
                private_media_root,
                project_settings,
                "PRIVATE_MEDIA_ROOT",
                Path(getattr(project_settings, "BASE_DIR", Path.cwd())) / "private_media",
            ),
            tag_name=tag_name,
            auth=auth,
            throttle=(throttle if throttle is not None else
                      getattr(project_settings, "THROTTLE_RATES", None)),
            safe_guard_docs_url=(safe_guard_docs_url if safe_guard_docs_url is not None else
                                 getattr(project_settings, "SAFE_GUARD_DOCS_URL", False)),
            docs_session_lifetime_seconds=(docs_session_lifetime_seconds if docs_session_lifetime_seconds is not None else
                                           getattr(project_settings, "DOCS_SESSION_LIFETIME_SECONDS", 3600)),
            docs_cookie_secure=_setting_override(
                docs_cookie_secure, project_settings, "DOCS_COOKIE_SECURE", None
            ),
            docs_login_max_attempts=(docs_login_max_attempts if docs_login_max_attempts is not None else
                                     getattr(project_settings, "DOCS_LOGIN_MAX_ATTEMPTS", 5)),
            docs_login_window_seconds=(docs_login_window_seconds if docs_login_window_seconds is not None else
                                       getattr(project_settings, "DOCS_LOGIN_WINDOW_SECONDS", 300)),
            admin_enabled=(admin_enabled if admin_enabled is not None else
                           getattr(project_settings, "ADMIN_ENABLED", False)),
            admin_url=admin_url or getattr(project_settings, "ADMIN_URL", "/admin/"),
            admin_session_lifetime_seconds=(admin_session_lifetime_seconds if admin_session_lifetime_seconds is not None else
                                            getattr(project_settings, "ADMIN_SESSION_LIFETIME_SECONDS", 3600)),
            admin_cookie_secure=_setting_override(
                admin_cookie_secure, project_settings, "ADMIN_COOKIE_SECURE", None
            ),
            admin_monitoring_enabled=(admin_monitoring_enabled if admin_monitoring_enabled is not None else
                                      getattr(project_settings, "ADMIN_MONITORING_ENABLED", False)),
            project_namespace=getattr(project_settings, "PROJECT_NAMESPACE", None),
        )
        self.project_name = getattr(project_settings, "PROJECT_NAME", None)
        self.project_namespace = getattr(project_settings, "PROJECT_NAMESPACE", None)
        self.project_dir = (
            Path(getattr(project_settings, "BASE_DIR", Path.cwd())).resolve()
            if project_settings is not None else None
        )
        if project_settings is not None:
            from .authentication import install_default_authentication

            install_default_authentication(self, project_settings)


def _setting_override(value: Any, settings: Any, name: str, default: Any) -> Any:
    """Use explicit values, including None; inherit settings only when omitted."""
    if value is not UNSET:
        return value
    return getattr(settings, name, default) if settings is not None else default
