"""Database-backed administrator accounts and sessions for private docs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import secrets
import time

from sqlalchemy import delete, inspect, insert, select

from .models import ADMIN_SESSION_TABLE, AdminUser
from .project import project_object_name
from .security import check_password, make_password


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def render_login_page(title: str, csrf_token: str, error: str | None = None) -> str:
    """Render the built-in administrator login page for protected docs."""
    safe_title = html.escape(title)
    error_html = (
        f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · {safe_title}</title><style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:28px;color:#f4f7ff;background:radial-gradient(ellipse at 15% 10%,#18386a 0,transparent 38%),radial-gradient(ellipse at 90% 90%,#263a6b 0,transparent 42%),#0a1020}}
.card{{width:min(100%,430px);padding:38px;border:1px solid #ffffff20;border-radius:24px;background:#101a2beF;box-shadow:0 30px 90px #0008}}
.mark{{width:46px;height:46px;display:grid;place-items:center;border-radius:14px;background:linear-gradient(135deg,#5eead4,#60a5fa);color:#07111e;font-weight:900;font-size:22px}}
h1{{font-size:25px;letter-spacing:-.03em;margin:25px 0 8px}}.sub{{color:#a9b5ca;margin:0 0 28px;line-height:1.55;font-size:14px}}
label{{display:block;font-size:13px;font-weight:650;margin:18px 0 8px;color:#dce5f4}}input{{display:block;width:100%;border:1px solid #ffffff26;border-radius:11px;padding:13px 14px;background:#080f1d;color:#f4f7ff;font:inherit;outline:none}}input:focus{{border-color:#60a5fa;box-shadow:0 0 0 3px #60a5fa25}}
button{{width:100%;margin-top:25px;padding:13px 16px;border:0;border-radius:11px;background:#7dd3fc;color:#07111e;font:inherit;font-weight:750;cursor:pointer}}button:hover{{background:#a5e4ff}}.error{{padding:11px 13px;border-radius:10px;background:#7f1d1d55;border:1px solid #f8717180;color:#fecaca;font-size:13px}}
.foot{{margin:22px 0 0;text-align:center;color:#8492a9;font-size:12px}}
</style></head><body><main class="card"><div class="mark" aria-hidden="true">L</div><h1>Sign in to {safe_title}</h1><p class="sub">Administrator access is required to view the API documentation.</p>{error_html}
<form method="post" action="/" autocomplete="on"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
<label for="username">Admin username</label><input id="username" name="username" autocomplete="username" required autofocus>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Continue to documentation</button></form><p class="foot">Protected documentation · {safe_title}</p></main></body></html>"""


class DocsGuard:
    """Persist docs administrators and expiring login sessions in the app DB."""

    def __init__(self, *, database=None, mongodb=None, session_lifetime_seconds=3600,
                 project_namespace: str | None = None):
        if database is None and mongodb is None:
            raise ValueError("Private docs access requires a configured database")
        if isinstance(session_lifetime_seconds, bool) or not isinstance(session_lifetime_seconds, int):
            raise ValueError("DOCS_SESSION_LIFETIME_SECONDS must be a positive integer")
        if session_lifetime_seconds <= 0:
            raise ValueError("DOCS_SESSION_LIFETIME_SECONDS must be a positive integer")
        self.database = database
        self.mongodb = mongodb
        self.session_lifetime_seconds = session_lifetime_seconds
        self.project_namespace = project_namespace

    def _object_name(self, name: str) -> str:
        return project_object_name(name, namespace=self.project_namespace)

    @staticmethod
    def _session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_admin(username: str, email: str, password: str) -> tuple[str, str, str]:
        username = username.strip()
        email = email.strip().lower()
        if not username or len(username) > 120:
            raise ValueError("username must contain 1 to 120 characters")
        if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
            raise ValueError("enter a valid email address")
        if not isinstance(password, str) or len(password) < 8 or len(password) > 128:
            raise ValueError("password must contain 8 to 128 characters")
        return username, email, password

    def _require_initialized(self) -> None:
        if self.mongodb is not None:
            names = set(self.mongodb.database.list_collection_names())
            required = {self._object_name("admin_user"), self._object_name("lenapi_admin_sessions")}
            if not required.issubset(names):
                raise RuntimeError("Run `python lenora.py migration` before creating an admin user")
        elif not inspect(self.database.engine).has_table(self._object_name("admin_user")) or not inspect(
            self.database.engine
        ).has_table(self._object_name("lenapi_admin_sessions")):
            raise RuntimeError("Run `python lenora.py migration` before creating an admin user")

    def _has_admin(self) -> bool:
        if self.mongodb is not None:
            return self.mongodb.collection(self._object_name("admin_user")).find_one({}, {"_id": 1}) is not None
        if not inspect(self.database.engine).has_table(self._object_name("admin_user")):
            return False
        with self.database.engine.connect() as connection:
            return connection.execute(select(AdminUser.id).limit(1)).first() is not None

    async def has_admin(self) -> bool:
        try:
            return await asyncio.to_thread(self._has_admin)
        except RuntimeError:
            # Preserve actionable setup errors such as "run migration first".
            raise
        except Exception as exc:
            raise RuntimeError("admin account database is unavailable") from exc

    def _has_superuser(self) -> bool:
        self._require_initialized()
        if self.mongodb is not None:
            return self.mongodb.collection(self._object_name("admin_user")).find_one(
                {"is_superuser": True}, {"_id": 1}
            ) is not None
        with self.database.engine.connect() as connection:
            return connection.execute(
                select(AdminUser.id).where(AdminUser.is_superuser.is_(True)).limit(1)
            ).first() is not None

    async def has_superuser(self) -> bool:
        try:
            return await asyncio.to_thread(self._has_superuser)
        except RuntimeError:
            # Do not hide a missing migration behind a generic DB error.
            raise
        except Exception as exc:
            raise RuntimeError("admin account database is unavailable") from exc

    def create_admin(self, username: str, email: str, password: str, *,
                     is_superuser: bool = False, permissions: dict | None = None) -> str:
        username, email, password = self._validate_admin(username, email, password)
        if not isinstance(is_superuser, bool):
            raise ValueError("is_superuser must be a boolean")
        if permissions is not None and not isinstance(permissions, dict):
            raise ValueError("permissions must be an object")
        self._require_initialized()
        password_hash = make_password(password)
        created_at = time.time()
        try:
            if self.mongodb is not None:
                result = self.mongodb.collection(self._object_name("admin_user")).insert_one({
                    "username": username,
                    "email": email,
                    "password": password_hash,
                    "created_at": created_at,
                    "is_superuser": is_superuser,
                    "permissions": json.dumps(permissions or {}),
                })
                return str(result.inserted_id)
            with self.database.engine.begin() as connection:
                result = connection.execute(insert(AdminUser.__table__).values(
                    username=username,
                    email=email,
                    password=password_hash,
                    created_at=created_at,
                    is_superuser=is_superuser,
                    permissions=json.dumps(permissions or {}),
                ))
                return str(result.inserted_primary_key[0])
        except Exception as exc:
            if self.mongodb is not None:
                try:
                    from pymongo.errors import DuplicateKeyError
                except ImportError:
                    DuplicateKeyError = ()
                if isinstance(exc, DuplicateKeyError):
                    raise ValueError("an admin with that username or email already exists") from exc
            else:
                from sqlalchemy.exc import IntegrityError
                if isinstance(exc, IntegrityError):
                    raise ValueError("an admin with that username or email already exists") from exc
            raise

    def _authenticate(self, username: str, password: str):
        self._require_initialized()
        if self.mongodb is not None:
            row = self.mongodb.collection(self._object_name("admin_user")).find_one({"username": username.strip()})
            if row is None or not check_password(password, row.get("password", "")):
                return None
            return {"id": str(row["_id"]), "username": row["username"]}
        with self.database.engine.connect() as connection:
            row = connection.execute(
                select(AdminUser.id, AdminUser.username, AdminUser.password).where(
                    AdminUser.username == username.strip()
                )
            ).first()
        if row is None or not check_password(password, row.password):
            return None
        return {"id": str(row.id), "username": row.username}

    async def authenticate(self, username: str, password: str):
        try:
            return await asyncio.to_thread(self._authenticate, username, password)
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise RuntimeError("admin authentication database is unavailable") from exc

    def _create_session(self, admin_id: str) -> str:
        self._require_initialized()
        session_id = secrets.token_urlsafe(32)
        digest = self._session_hash(session_id)
        now = time.time()
        expires_at = now + self.session_lifetime_seconds
        if self.mongodb is not None:
            sessions = self.mongodb.collection(self._object_name("lenapi_admin_sessions"))
            sessions.delete_many({"expires_at": {"$lte": now}})
            sessions.insert_one({
                "_id": digest,
                "admin_user_id": admin_id,
                "expires_at": expires_at,
            })
        else:
            with self.database.engine.begin() as connection:
                connection.execute(delete(ADMIN_SESSION_TABLE).where(
                    ADMIN_SESSION_TABLE.c.expires_at <= now
                ))
                connection.execute(insert(ADMIN_SESSION_TABLE).values(
                    session_hash=digest,
                    admin_user_id=int(admin_id),
                    expires_at=expires_at,
                ))
        return session_id

    async def create_session(self, admin_id: str) -> str:
        try:
            return await asyncio.to_thread(self._create_session, admin_id)
        except Exception as exc:
            raise RuntimeError("could not create admin session in the configured database") from exc

    def _resolve_session(self, session_id: str):
        self._require_initialized()
        digest = self._session_hash(session_id)
        now = time.time()
        if self.mongodb is not None:
            sessions = self.mongodb.collection(self._object_name("lenapi_admin_sessions"))
            row = sessions.find_one({"_id": digest})
            if row is None:
                return None
            if row.get("expires_at", 0) <= now:
                sessions.delete_one({"_id": digest})
                return None
            from .mongodb import _object_id

            admin = self.mongodb.collection(self._object_name("admin_user")).find_one({
                "_id": _object_id(row["admin_user_id"])
            })
            return None if admin is None else {"id": str(admin["_id"]), "username": admin["username"]}
        with self.database.engine.begin() as connection:
            row = connection.execute(
                select(AdminUser.id, AdminUser.username, ADMIN_SESSION_TABLE.c.expires_at)
                .join(ADMIN_SESSION_TABLE, ADMIN_SESSION_TABLE.c.admin_user_id == AdminUser.id)
                .where(ADMIN_SESSION_TABLE.c.session_hash == digest)
            ).first()
            if row is None:
                return None
            if row.expires_at <= now:
                connection.execute(delete(ADMIN_SESSION_TABLE).where(
                    ADMIN_SESSION_TABLE.c.session_hash == digest
                ))
                return None
            return {"id": str(row.id), "username": row.username}

    async def resolve_session(self, session_id: str | None):
        if not session_id:
            return None
        try:
            return await asyncio.to_thread(self._resolve_session, session_id)
        except Exception as exc:
            raise RuntimeError("admin session database is unavailable") from exc

    def _delete_session(self, session_id: str) -> None:
        digest = self._session_hash(session_id)
        if self.mongodb is not None:
            self.mongodb.collection(self._object_name("lenapi_admin_sessions")).delete_one({"_id": digest})
        else:
            with self.database.engine.begin() as connection:
                connection.execute(delete(ADMIN_SESSION_TABLE).where(
                    ADMIN_SESSION_TABLE.c.session_hash == digest
                ))

    async def delete_session(self, session_id: str | None) -> None:
        if session_id:
            try:
                await asyncio.to_thread(self._delete_session, session_id)
            except Exception as exc:
                raise RuntimeError("could not delete admin session from the configured database") from exc
