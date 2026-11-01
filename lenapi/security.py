"""Small HS256 JWT authentication helper for protected lenApi routes."""

import base64
import binascii
import hashlib
import hmac as _hmac
import json
import math
import re
import secrets
import threading
import time
from typing import Any

from pydantic import BaseModel, Field, create_model

from .concurrency import call_sync_or_async
from .request import Request
from .response import HTTPException, Response


_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(value: str) -> bytes:
    if not value or not _B64URL.fullmatch(value):
        raise ValueError("Malformed token segment")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    """Hash a password in Django's ``pbkdf2_sha256`` encoded format."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    salt = "".join(secrets.choice(alphabet) for _ in range(22))
    iterations = 1_800_000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    )
    encoded = base64.b64encode(digest).decode("ascii").strip()
    return f"pbkdf2_sha256${iterations}${salt}${encoded}"


def verify_password(password: str, encoded_password: str) -> bool:
    """Verify a password against lenApi or Django PBKDF2 encoded storage."""
    if not isinstance(password, str) or not isinstance(encoded_password, str):
        return False
    try:
        # Verify hashes from the earlier lenApi scrypt helper during upgrades.
        if encoded_password.startswith("scrypt$"):
            algorithm, n, r, p, salt_text, digest_text = encoded_password.split("$")
            if (algorithm, int(n), int(r), int(p)) != ("scrypt", 16384, 8, 1):
                return False
            salt, expected = _decode_segment(salt_text), _decode_segment(digest_text)
            if len(salt) != 16 or len(expected) != 64:
                return False
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
            return _hmac.compare_digest(actual, expected)
        algorithm, iterations_text, salt, expected = encoded_password.split("$", 3)
        iterations = int(iterations_text)
        if algorithm != "pbkdf2_sha256" or not 1 <= iterations <= 5_000_000:
            return False
        if not salt.isalnum():
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
        )
        actual_encoded = base64.b64encode(actual).decode("ascii").strip()
        return _hmac.compare_digest(actual_encoded, expected)
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def make_password(password: str) -> str:
    """Create a Django-compatible one-way password hash."""
    return hash_password(password)


def check_password(password: str, encoded_password: str) -> bool:
    """Return whether a raw password matches its stored password hash."""
    return verify_password(password, encoded_password)


def make_api_key() -> str:
    """Generate an API key from 32 bytes of cryptographically secure randomness."""
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """Return a deterministic SHA-256 digest for storing an API key safely."""
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class JWTAuth:
    """Create HS256 access tokens and use the instance as a Bearer dependency.

    ``secret_key`` should be a high-entropy secret read from an environment
    variable. This helper only supports HS256 and validates signatures and
    expiration claims; it does not authenticate user credentials itself.
    """

    algorithm = "HS256"

    def __init__(
        self,
        secret_key: str | bytes,
        *,
        token_lifetime_seconds: int = 3600,
        refresh_token_lifetime_seconds: int = 60 * 60 * 24 * 30,
        issuer: str | None = None,
        authenticate=None,
        authenticate_async=None,
        security_store=None,
        session_store=None,
        allow_multiple_sessions: bool = True,
        login_field: str = "username",
    ) -> None:
        key = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("HS256 secret_key must be at least 32 bytes")
        if token_lifetime_seconds <= 0:
            raise ValueError("token_lifetime_seconds must be positive")
        if refresh_token_lifetime_seconds <= 0:
            raise ValueError("refresh_token_lifetime_seconds must be positive")
        if not isinstance(login_field, str) or not login_field.isidentifier() or login_field == "password":
            raise ValueError("login_field must be a valid model field name other than 'password'")
        if not isinstance(allow_multiple_sessions, bool):
            raise TypeError("allow_multiple_sessions must be a boolean")
        if not allow_multiple_sessions and security_store is None and session_store is None:
            raise ValueError("Single-session JWT authentication requires a configured security store or cache")
        self.secret_key = key
        self.token_lifetime_seconds = token_lifetime_seconds
        self.refresh_token_lifetime_seconds = refresh_token_lifetime_seconds
        self.issuer = issuer
        self.authenticate = authenticate
        self._authenticate_async = authenticate_async
        self.security_store = security_store
        self.session_store = session_store if session_store is not None else security_store
        self.allow_multiple_sessions = allow_multiple_sessions
        self.login_field = login_field
        self._refresh_namespace = hashlib.sha256(key).hexdigest()[:32]
        self._used_refresh_tokens: dict[str, float] = {}
        self._refresh_lock = threading.Lock()

    async def authenticate_async(self, identifier: str, password: str):
        """Verify login credentials asynchronously.

        Uses the configured async credential callback when available. For
        sync-only callbacks, runs them in a worker thread so async routes do
        not block the event loop.
        """
        authenticate = self._authenticate_async or self.authenticate
        if authenticate is None:
            raise ValueError("No credential authentication callback is configured")
        return await call_sync_or_async(authenticate, identifier, password)

    def create_access_token(
        self,
        subject: str,
        *,
        claims: dict[str, Any] | None = None,
        expires_in: int | None = None,
    ) -> str:
        """Sign a token for a subject, adding issued-at and expiration claims."""
        if not self.allow_multiple_sessions:
            raise RuntimeError(
                "Single-session JWT mode requires `await auth.issue_tokens(subject)` "
                "so the active session can be recorded"
            )
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        lifetime = self.token_lifetime_seconds if expires_in is None else expires_in
        if lifetime <= 0:
            raise ValueError("expires_in must be positive")
        payload = dict(claims or {})
        reserved = {"sub", "iat", "exp", "iss", "token_use", "jti", "sid"}
        if reserved.intersection(payload):
            raise ValueError("claims cannot override reserved token claims")
        return self._create_token(subject, payload, lifetime, token_use="access")

    def create_refresh_token(
        self,
        subject: str,
        *,
        claims: dict[str, Any] | None = None,
        expires_in: int | None = None,
    ) -> str:
        """Create a refresh token that cannot be used as a bearer access token."""
        if not self.allow_multiple_sessions:
            raise RuntimeError(
                "Single-session JWT mode requires `await auth.issue_tokens(subject)` "
                "so the active session can be recorded"
            )
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        lifetime = self.refresh_token_lifetime_seconds if expires_in is None else expires_in
        if lifetime <= 0:
            raise ValueError("expires_in must be positive")
        payload = dict(claims or {})
        reserved = {"sub", "iat", "exp", "iss", "token_use", "jti", "sid"}
        if reserved.intersection(payload):
            raise ValueError("claims cannot override reserved token claims")
        payload["jti"] = secrets.token_urlsafe(32)
        return self._create_token(subject, payload, lifetime, token_use="refresh")

    async def issue_tokens(
        self,
        subject: str,
        *,
        claims: dict[str, Any] | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        """Issue an access/refresh pair and apply the configured login policy.

        In single-session mode, the newest login replaces the user's active
        session in the shared security store. Older access and refresh tokens
        are rejected on subsequent use.
        """
        if self.allow_multiple_sessions:
            return {
                "access_token": self.create_access_token(
                    subject, claims=claims, expires_in=expires_in
                ),
                "refresh_token": self.create_refresh_token(subject, claims=claims),
                "token_type": "bearer",
                "expires_in": self.token_lifetime_seconds if expires_in is None else expires_in,
            }
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        lifetime = self.token_lifetime_seconds if expires_in is None else expires_in
        if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
            raise ValueError("expires_in must be a positive integer")
        payload = dict(claims or {})
        reserved = {"sub", "iat", "exp", "iss", "token_use", "jti", "sid"}
        if reserved.intersection(payload):
            raise ValueError("claims cannot override reserved token claims")
        session_id = secrets.token_urlsafe(32)
        token_claims = {**payload, "sid": session_id}
        access_token = self._create_token(subject, token_claims, lifetime, token_use="access")
        refresh_claims = {**token_claims, "jti": secrets.token_urlsafe(32)}
        refresh_token = self._create_token(
            subject, refresh_claims, self.refresh_token_lifetime_seconds,
            token_use="refresh",
        )
        await call_sync_or_async(self.session_store.set,
            self._active_session_key(subject), session_id,
            ttl=self.refresh_token_lifetime_seconds,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": lifetime,
        }

    def _active_session_key(self, subject: str) -> str:
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        return f"jwt_active_session:{self._refresh_namespace}:{subject_hash}"

    async def _require_active_session(self, payload: dict[str, Any], *, session_store=None) -> None:
        if self.allow_multiple_sessions:
            return
        session_id = payload.get("sid")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Token is not associated with an active login session")
        store = session_store if session_store is not None else self.session_store
        if store is None:
            raise HTTPException(503, "Authentication session storage is unavailable")
        try:
            active_session = await call_sync_or_async(
                store.get, self._active_session_key(payload["sub"])
            )
        except RuntimeError as exc:
            raise HTTPException(503, "Authentication session storage is unavailable") from exc
        if not isinstance(active_session, str) or not secrets.compare_digest(active_session, session_id):
            raise ValueError("Token session has been replaced or revoked")

    def _create_token(self, subject: str, payload: dict[str, Any], lifetime: int, *, token_use: str) -> str:
        now = int(time.time())
        payload.update({"sub": subject, "iat": now, "exp": now + lifetime, "token_use": token_use})
        if self.issuer is not None:
            payload["iss"] = self.issuer
        header = {"alg": self.algorithm, "typ": "JWT"}
        head = _encode_segment(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        body = _encode_segment(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{head}.{body}".encode("ascii")
        signature = _hmac.new(self.secret_key, signing_input, hashlib.sha256).digest()
        return f"{head}.{body}.{_encode_segment(signature)}"

    async def refresh_access_token(self, refresh_token: str, *, security_store=None) -> str:
        """Consume a refresh token once and issue an access token.

        When a shared security store is configured, atomic storage prevents
        replay across workers. Without one, replay protection is process-local.
        """
        payload = self.decode_token(refresh_token, allow_refresh=True)
        if payload.get("token_use") != "refresh":
            raise ValueError("A refresh token is required")
        await self._require_active_session(payload)
        await self._consume_refresh_token(payload, security_store=security_store)
        claims = {key: value for key, value in payload.items()
                  if key not in {"sub", "iat", "exp", "iss", "token_use", "jti"}}
        if self.allow_multiple_sessions:
            return self.create_access_token(payload["sub"], claims=claims)
        return self._create_token(
            payload["sub"], claims, self.token_lifetime_seconds, token_use="access"
        )

    async def _consume_refresh_token(self, payload: dict[str, Any], *, security_store=None) -> None:
        token_id = payload.get("jti")
        expiration = payload.get("exp")
        if not isinstance(token_id, str) or not token_id:
            raise ValueError("Refresh token is missing its one-time-use identifier")
        if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
            raise ValueError("Refresh token has an invalid expiration")
        ttl = max(1, math.ceil(expiration - time.time()))
        store = security_store if security_store is not None else self.security_store
        key = f"jwt_refresh_used:{self._refresh_namespace}:{token_id}"
        if store is not None:
            used = await call_sync_or_async(store.claim, key, ttl)
            if used:
                raise ValueError("Refresh token has already been used")
            return
        now = time.time()
        with self._refresh_lock:
            self._used_refresh_tokens = {
                item: expires_at for item, expires_at in self._used_refresh_tokens.items()
                if expires_at > now
            }
            if token_id in self._used_refresh_tokens:
                raise ValueError("Refresh token has already been used")
            self._used_refresh_tokens[token_id] = expiration

    def router(self, authenticate=None, *, throttle: dict[str, str] | None = None,
               security_store=None):
        """Build `/auth/token` and `/auth/refresh` routes for an app.

        `authenticate(identifier, password)` may be supplied here or when
        constructing the instance. The identifier field name is controlled by
        `login_field`. The callback must return the user's string ID, or
        `(string_id, claims)`, when credentials are valid; otherwise return None.
        """
        from .application import Router

        if authenticate is None and self.authenticate is None and self._authenticate_async is None:
            raise ValueError("JWT token routes require an authenticate(identifier, password) callback")
        authenticate = authenticate or self.authenticate_async

        # Keep password verification endpoints rate-limited even when the app
        # has no global throttle configured. An empty mapping explicitly opts
        # out; applications can also supply a stricter policy here.
        token_throttle = {"ip": "10/minute"} if throttle is None else throttle
        refresh_store = security_store if security_store is not None else self.security_store
        router = Router(prefix="/auth")
        credentials_model = create_model(
            f"JWTTokenCredentials_{self.login_field}",
            **{
                self.login_field: (str, Field(min_length=1)),
                "password": (str, Field(min_length=1)),
            },
        )

        @router.post("/token", include_in_schema=False, throttle=token_throttle)
        async def issue_token(credentials: credentials_model):
            user = await call_sync_or_async(
                authenticate, getattr(credentials, self.login_field), credentials.password
            )
            if user is None or user is False:
                raise HTTPException(401, "Invalid login credentials")
            if isinstance(user, tuple) and len(user) == 2:
                subject, claims = user
            else:
                subject, claims = user, None
            if not isinstance(subject, str) or not subject:
                raise TypeError("authenticate must return a non-empty string user ID")
            try:
                return await self.issue_tokens(subject, claims=claims)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(503, "Could not create an authentication session") from exc

        @router.post("/refresh", include_in_schema=False, throttle=token_throttle)
        async def refresh_token(body: _RefreshRequest):
            try:
                access_token = await self.refresh_access_token(
                    body.refresh_token, security_store=refresh_store
                )
                payload = self.decode_token(body.refresh_token, allow_refresh=True)
            except ValueError as exc:
                raise HTTPException(401, "Invalid or expired refresh token") from exc
            claims = {key: value for key, value in payload.items()
                      if key not in {"sub", "iat", "exp", "iss", "token_use", "jti"}}
            if self.allow_multiple_sessions:
                new_refresh_token = self.create_refresh_token(payload["sub"], claims=claims)
            else:
                if not isinstance(claims.get("sid"), str):
                    raise HTTPException(401, "Invalid or expired refresh token")
                refresh_claims = {**claims, "jti": secrets.token_urlsafe(32)}
                new_refresh_token = self._create_token(
                    payload["sub"], refresh_claims,
                    self.refresh_token_lifetime_seconds, token_use="refresh",
                )
            return {
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "token_type": "bearer",
                "expires_in": self.token_lifetime_seconds,
            }

        return router

    def decode_token(self, token: str, *, allow_refresh: bool = False) -> dict[str, Any]:
        """Verify and decode a token, raising ValueError if it is invalid."""
        try:
            header_segment, payload_segment, signature_segment = token.split(".")
            header = json.loads(_decode_segment(header_segment))
            payload = json.loads(_decode_segment(payload_segment))
            signature = _decode_segment(signature_segment)
            if not isinstance(header, dict) or header.get("alg") != self.algorithm:
                raise ValueError("Unsupported token algorithm")
            if not isinstance(payload, dict):
                raise ValueError("Token payload must be an object")
            signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
            expected = _hmac.new(self.secret_key, signing_input, hashlib.sha256).digest()
            if not _hmac.compare_digest(signature, expected):
                raise ValueError("Invalid token signature")
            expiration = payload.get("exp")
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
                raise ValueError("Token must have a numeric exp claim")
            if expiration <= time.time():
                raise ValueError("Token has expired")
            not_before = payload.get("nbf")
            if not_before is not None:
                if isinstance(not_before, bool) or not isinstance(not_before, (int, float)):
                    raise ValueError("Token nbf claim must be numeric")
                if not_before > time.time():
                    raise ValueError("Token is not active yet")
            if self.issuer is not None and payload.get("iss") != self.issuer:
                raise ValueError("Invalid token issuer")
            if payload.get("token_use") == "refresh" and not allow_refresh:
                raise ValueError("Refresh token cannot be used as an access token")
            if not isinstance(payload.get("sub"), str) or not payload["sub"]:
                raise ValueError("Token must have a non-empty sub claim")
            return payload
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed token") from exc

    async def __call__(self, request: Request) -> dict[str, Any]:
        """Resolve a verified Bearer token for use with ``Depends``."""
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        try:
            payload = self.decode_token(token.strip())
            await self._require_active_session(payload)
            return payload
        except ValueError as exc:
            raise HTTPException(
                401,
                "Invalid or expired bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc


class _Credentials(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class _RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class APIKeyAuth:
    """Check a shared API key or resolve a user-specific key from a callback."""

    def __init__(self, api_key: str | None = None, *, header_name: str = "x-api-key", authenticate=None) -> None:
        if authenticate is None and (not isinstance(api_key, str) or len(api_key.encode("utf-8")) < 32):
            raise ValueError("API key must be at least 32 bytes")
        if not header_name or any(character.isspace() for character in header_name):
            raise ValueError("header_name must be a valid header name")
        self.api_key = api_key.encode("utf-8") if api_key is not None else None
        self.authenticate = authenticate
        self.header_name = header_name.lower()

    async def __call__(self, request: Request) -> Any:
        supplied = request.headers.get(self.header_name, "")
        if self.authenticate is not None:
            identity = await call_sync_or_async(self.authenticate, supplied)
            if identity is not None and identity is not False:
                return identity
        elif supplied and _hmac.compare_digest(supplied.encode("utf-8"), self.api_key):
            return {"auth_method": "api_key"}
        raise HTTPException(401, "Invalid or missing API key", headers={"WWW-Authenticate": "ApiKey"})


class BasicAuth:
    """Verify HTTP Basic credentials with an app-provided callback."""

    def __init__(self, authenticate) -> None:
        self.authenticate = authenticate

    async def __call__(self, request: Request) -> Any:
        authorization = request.headers.get("authorization", "")
        scheme, separator, value = authorization.partition(" ")
        if not separator or scheme.lower() != "basic":
            raise HTTPException(401, "Basic credentials required", headers={"WWW-Authenticate": 'Basic realm="lenApi"'})
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(401, "Invalid Basic credentials", headers={"WWW-Authenticate": 'Basic realm="lenApi"'}) from exc
        if not username or not password:
            raise HTTPException(401, "Invalid Basic credentials", headers={"WWW-Authenticate": 'Basic realm="lenApi"'})
        user = await call_sync_or_async(self.authenticate, username, password)
        if user is None or user is False:
            raise HTTPException(401, "Invalid username or password", headers={"WWW-Authenticate": 'Basic realm="lenApi"'})
        return user


class SessionAuth:
    """Resolve an HttpOnly session cookie through a server-side session store."""

    def __init__(
        self, lookup=None, *, store=None, cache=None, cookie_name: str = "lenapi_session",
        allow_multiple_sessions: bool = True,
    ) -> None:
        if lookup is None and store is None and cache is None:
            raise ValueError("SessionAuth requires a lookup callback, security store, or cache")
        if store is not None and cache is not None:
            raise ValueError("Pass either store or cache to SessionAuth, not both")
        if not isinstance(allow_multiple_sessions, bool):
            raise TypeError("allow_multiple_sessions must be a boolean")
        if not allow_multiple_sessions and store is None and cache is None:
            raise ValueError("Single-session authentication requires a shared store or cache")
        self.lookup = lookup
        self.store = store
        self.cache = cache
        self._state_store = cache if cache is not None else store
        self.cookie_name = cookie_name
        self.allow_multiple_sessions = allow_multiple_sessions

    @staticmethod
    def _identity_subject(identity: Any) -> str:
        if not isinstance(identity, dict):
            raise ValueError("Session identity must be a dictionary with a user ID")
        for field in ("sub", "user_id", "id"):
            value = identity.get(field)
            if value is not None and str(value):
                return str(value)
        raise ValueError("Session identity must include 'sub', 'user_id', or 'id'")

    def _active_session_key(self, identity: dict[str, Any]) -> str:
        subject_hash = hashlib.sha256(
            self._identity_subject(identity).encode("utf-8")
        ).hexdigest()
        return f"session_active:{self.cookie_name}:{subject_hash}"

    async def create_session(self, identity: dict[str, Any], *, ttl: int = 3600) -> str:
        """Create a session ID and store its identity with the configured policy."""
        if self._state_store is None:
            raise RuntimeError("Creating sessions requires a configured security store or cache")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("ttl must be a positive integer")
        if not isinstance(identity, dict):
            raise TypeError("identity must be a dictionary")
        active_session_key = (
            self._active_session_key(identity)
            if not self.allow_multiple_sessions else None
        )
        previous_session_id = None
        if active_session_key is not None:
            previous_session_id = await call_sync_or_async(
                self._state_store.get, active_session_key
            )
        session_id = secrets.token_urlsafe(32)
        stored_identity = identity if self.cache is not None else json.dumps(
            identity, separators=(",", ":")
        )
        await call_sync_or_async(
            self._state_store.set, f"session:{session_id}", stored_identity, ttl=ttl,
        )
        if active_session_key is not None:
            await call_sync_or_async(
                self._state_store.set, active_session_key, session_id, ttl=ttl
            )
            if (
                isinstance(previous_session_id, str)
                and previous_session_id != session_id
            ):
                await call_sync_or_async(
                    self._state_store.delete, f"session:{previous_session_id}"
                )
        return session_id

    async def __call__(self, request: Request) -> Any:
        session_id = request.cookies.get(self.cookie_name)
        if session_id and self._state_store is not None:
            try:
                serialized = await call_sync_or_async(
                    self._state_store.get, f"session:{session_id}"
                )
            except RuntimeError as exc:
                raise HTTPException(503, "Session storage is unavailable") from exc
            if isinstance(serialized, dict):
                identity = serialized
            else:
                try:
                    identity = json.loads(serialized) if serialized is not None else None
                except (TypeError, json.JSONDecodeError):
                    identity = None
            if identity is not None and not self.allow_multiple_sessions:
                try:
                    active_id = await call_sync_or_async(
                        self._state_store.get, self._active_session_key(identity)
                    )
                except (TypeError, ValueError):
                    active_id = None
                except RuntimeError as exc:
                    raise HTTPException(503, "Session storage is unavailable") from exc
                if not isinstance(active_id, str) or not secrets.compare_digest(active_id, session_id):
                    identity = None
        else:
            identity = (
                await call_sync_or_async(self.lookup, session_id)
                if session_id and self.lookup is not None else None
            )
        if identity is None or identity is False:
            raise HTTPException(401, "Valid session required")
        return identity


class OpaqueBearerAuth:
    """Resolve an opaque bearer token through an application-provided token store."""

    def __init__(self, lookup=None, *, store=None) -> None:
        if lookup is None and store is None:
            raise ValueError("OpaqueBearerAuth requires a lookup callback or security store")
        self.lookup = lookup
        self.store = store

    async def __call__(self, request: Request) -> Any:
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        identity = None
        if separator and scheme.lower() == "bearer" and token.strip():
            if self.store is not None:
                serialized = await call_sync_or_async(
                    self.store.get, f"opaque_bearer:{token.strip()}"
                )
                try:
                    identity = json.loads(serialized) if serialized is not None else None
                except (TypeError, json.JSONDecodeError):
                    identity = None
            else:
                identity = await call_sync_or_async(self.lookup, token.strip())
        if identity is None or identity is False:
            raise HTTPException(401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})
        return identity


class OIDCAuth(OpaqueBearerAuth):
    """Validate an OIDC access token through a provider SDK or token introspector."""


class WebhookAuth:
    """Verify a raw-body HMAC webhook signature and optionally deduplicate events."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        security_store=None,
        provider: str = "default",
        signature_header: str = "x-webhook-signature",
        event_id_header: str | None = "x-webhook-id",
        timestamp_header: str | None = None,
        tolerance_seconds: int = 300,
        deduplication_ttl: int = 7 * 24 * 60 * 60,
    ) -> None:
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(self.secret) < 32:
            raise ValueError("Webhook secret must be at least 32 bytes")
        if tolerance_seconds <= 0 or deduplication_ttl <= 0:
            raise ValueError("Webhook time limits must be positive")
        self.security_store = security_store
        self.provider = provider
        self.signature_header = signature_header.lower()
        self.event_id_header = event_id_header.lower() if event_id_header else None
        self.timestamp_header = timestamp_header.lower() if timestamp_header else None
        self.tolerance_seconds = tolerance_seconds
        self.deduplication_ttl = deduplication_ttl

    async def __call__(self, request: Request) -> dict[str, str]:
        headers = request.headers
        supplied = headers.get(self.signature_header, "")
        signature = supplied.removeprefix("sha256=").strip()
        body = await request.body()
        signed_content = body
        if self.timestamp_header:
            timestamp = headers.get(self.timestamp_header, "")
            if not timestamp.isascii():
                raise HTTPException(401, "Invalid webhook timestamp")
            try:
                event_time = int(timestamp)
            except ValueError as exc:
                raise HTTPException(401, "Invalid webhook timestamp") from exc
            if abs(time.time() - event_time) > self.tolerance_seconds:
                raise HTTPException(401, "Webhook signature has expired")
            signed_content = timestamp.encode("ascii") + b"." + body
        expected = _hmac.new(self.secret, signed_content, hashlib.sha256).hexdigest()
        if not signature or not _hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid webhook signature")

        event_id = headers.get(self.event_id_header, "") if self.event_id_header else ""
        if self.security_store is not None and self.event_id_header:
            if not event_id:
                raise HTTPException(400, "Webhook event ID required")
            duplicate = await self.security_store.claim(
                f"webhook:{self.provider}:{event_id}", self.deduplication_ttl
            )
            if duplicate:
                # Acknowledge retries so providers stop redelivering an event
                # that this application has already accepted.
                raise HTTPException(200, "Webhook event already received")
        return {"auth_method": "webhook", "event_id": event_id}


def session_auth(
    lookup=None, *, store=None, cache=None, cookie_name: str = "lenapi_session",
    allow_multiple_sessions: bool = True,
) -> SessionAuth:
    """Create a session-cookie dependency with a callback, security store, or cache."""
    return SessionAuth(
        lookup, store=store, cache=cache, cookie_name=cookie_name,
        allow_multiple_sessions=allow_multiple_sessions,
    )


def opaque_bearer_auth(token_lookup=None, *, store=None) -> OpaqueBearerAuth:
    """Create an opaque bearer dependency with a callback or shared security store."""
    return OpaqueBearerAuth(token_lookup, store=store)


def oidc_auth(token_lookup) -> OIDCAuth:
    """Create an optional per-route OIDC token authentication dependency."""
    return OIDCAuth(token_lookup)


def webhook_auth(secret: str | bytes, *, security_store=None, **options) -> WebhookAuth:
    """Create a webhook signature and idempotency dependency."""
    return WebhookAuth(secret, security_store=security_store, **options)


class CSRFProtect:
    """Signed double-submit CSRF protection for cookie-authenticated routes."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    def __init__(
        self,
        secret_key: str | bytes,
        *,
        cookie_name: str = "lenapi_csrf",
        header_name: str = "x-csrf-token",
        secure: bool = True,
        samesite: str = "lax",
    ) -> None:
        self.secret = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key
        if len(self.secret) < 32:
            raise ValueError("CSRF secret_key must be at least 32 bytes")
        self.cookie_name = cookie_name
        self.header_name = header_name.lower()
        self.secure = secure
        self.samesite = samesite

    def generate_token(self) -> str:
        nonce = secrets.token_urlsafe(32)
        signature = _hmac.new(self.secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{nonce}.{signature}"

    def set_cookie(self, response: Response, *, max_age: int = 3600) -> str:
        """Set a browser-readable CSRF cookie and return its token for clients."""
        token = self.generate_token()
        response.set_cookie(
            self.cookie_name,
            token,
            max_age=max_age,
            secure=self.secure,
            httponly=False,
            samesite=self.samesite,
            path="/",
        )
        return token

    async def __call__(self, request: Request) -> bool:
        if request.method.upper() in self.SAFE_METHODS:
            return True
        cookie_token = request.cookies.get(self.cookie_name, "")
        header_token = request.headers.get(self.header_name, "")
        nonce, separator, supplied_signature = cookie_token.rpartition(".")
        if not separator or not nonce or not header_token:
            raise HTTPException(403, "CSRF token required")
        expected_signature = _hmac.new(self.secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
        if (
            not _hmac.compare_digest(cookie_token, header_token)
            or not _hmac.compare_digest(supplied_signature, expected_signature)
        ):
            raise HTTPException(403, "Invalid CSRF token")
        return True


def hmac_request_signature(
    secret: str | bytes, method: str, path: str, timestamp: str, body: bytes, *, nonce: str = ""
) -> str:
    """Create a hex HMAC-SHA256 signature for a lenApi signed request."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    body_digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{method.upper()}\n{path}\n{body_digest}\n{nonce}".encode("utf-8")
    return _hmac.new(key, message, hashlib.sha256).hexdigest()


class HMACAuth:
    """Authenticate timestamped, body-signed requests from trusted clients."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        tolerance_seconds: int = 300,
        nonce_store=None,
        header_name: str = "X-LenAPI-Signature",
    ) -> None:
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(self.secret) < 32:
            raise ValueError("HMAC secret must be at least 32 bytes")
        if tolerance_seconds <= 0:
            raise ValueError("tolerance_seconds must be positive")
        if (
            not isinstance(header_name, str)
            or not header_name.strip()
            or any(character.isspace() for character in header_name)
        ):
            raise ValueError("header_name must be a non-empty header name without whitespace")
        self.tolerance_seconds = tolerance_seconds
        self.nonce_store = nonce_store
        self.header_name = header_name.strip().lower()
        self._seen_nonces: dict[str, int] = {}
        self._nonce_lock = threading.Lock()

    async def __call__(self, request: Request) -> dict[str, str]:
        headers = request.headers
        timestamp = headers.get("x-lenapi-timestamp", "")
        supplied = headers.get(self.header_name, "")
        nonce = headers.get("x-lenapi-nonce", "")
        if not nonce or len(nonce) > 128:
            raise HTTPException(401, "Invalid signed request", headers={"WWW-Authenticate": "HMAC"})
        try:
            request_time = int(timestamp)
        except ValueError as exc:
            raise HTTPException(401, "Invalid signed request", headers={"WWW-Authenticate": "HMAC"}) from exc
        if abs(time.time() - request_time) > self.tolerance_seconds:
            raise HTTPException(401, "Signed request has expired", headers={"WWW-Authenticate": "HMAC"})
        body = await request.body()
        query = request.scope.get("query_string", b"").decode("latin-1")
        path = request.path + (f"?{query}" if query else "")
        expected = hmac_request_signature(self.secret, request.method, path, timestamp, body, nonce=nonce)
        if not _hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "Invalid signed request", headers={"WWW-Authenticate": "HMAC"})
        if self.nonce_store is not None:
            used = await call_sync_or_async(
                self.nonce_store.claim, f"hmac_nonce:{nonce}", self.tolerance_seconds
            )
            if used:
                raise HTTPException(401, "Signed request has already been used", headers={"WWW-Authenticate": "HMAC"})
        else:
            with self._nonce_lock:
                self._seen_nonces = {
                    seen_nonce: seen_at for seen_nonce, seen_at in self._seen_nonces.items()
                    if request_time - seen_at <= self.tolerance_seconds
                }
                if nonce in self._seen_nonces:
                    raise HTTPException(401, "Signed request has already been used", headers={"WWW-Authenticate": "HMAC"})
                self._seen_nonces[nonce] = request_time
        return {"auth_method": "hmac"}


def hmac_auth(
    secret: str | bytes,
    *,
    tolerance_seconds: int = 300,
    nonce_store=None,
    header_name: str = "X-LenAPI-Signature",
) -> HMACAuth:
    """Create an optional per-route HMAC request authentication dependency."""
    return HMACAuth(
        secret,
        tolerance_seconds=tolerance_seconds,
        nonce_store=nonce_store,
        header_name=header_name,
    )


def create_authenticator(
    settings, *, authenticate=None, authenticate_api_key=None, session_lookup=None,
    token_lookup=None, oidc_token_lookup=None, security_store=None, cache=None,
):
    """Create the authentication dependency selected in a project settings module."""
    mode = getattr(settings, "AUTHENTICATION", "jwt").lower()
    if mode == "jwt":
        token = settings.TOKEN
        return JWTAuth(
            token["SECRET_KEY"],
            token_lifetime_seconds=token.get("LIFETIME_SECONDS", 3600),
            refresh_token_lifetime_seconds=token.get("REFRESH_LIFETIME_SECONDS", 60 * 60 * 24 * 30),
            issuer=token.get("ISSUER"),
            security_store=security_store,
            session_store=cache if cache is not None else security_store,
            allow_multiple_sessions=getattr(settings, "ALLOW_MULTIPLE_SESSIONS", True),
        )
    if mode == "api_key":
        return APIKeyAuth(getattr(settings, "API_KEY", None), authenticate=authenticate_api_key)
    if mode == "basic_login":
        if authenticate is None:
            raise ValueError("basic_login requires an authenticate(username, password) callback")
        return BasicAuth(authenticate)
    if mode == "session":
        session = getattr(settings, "SESSION", {})
        lookup = session_lookup or getattr(settings, "SESSION_LOOKUP", None)
        if lookup is None and security_store is None:
            raise ValueError("session authentication requires security_store or session_lookup")
        return SessionAuth(
            lookup,
            store=security_store if lookup is None and cache is None else None,
            cache=cache if lookup is None else None,
            cookie_name=session.get("COOKIE_NAME", "lenapi_session"),
            allow_multiple_sessions=session.get(
                "ALLOW_MULTIPLE_SESSIONS",
                getattr(settings, "ALLOW_MULTIPLE_SESSIONS", True),
            ),
        )
    if mode == "opaque_bearer":
        lookup = token_lookup or getattr(settings, "TOKEN_LOOKUP", None)
        if lookup is None and security_store is None:
            raise ValueError("opaque_bearer authentication requires security_store or token_lookup")
        return OpaqueBearerAuth(lookup, store=security_store if lookup is None else None)
    if mode == "hmac":
        secret = getattr(settings, "HMAC_SECRET", None)
        if not secret:
            raise ValueError("hmac authentication requires HMAC_SECRET")
        return HMACAuth(secret, tolerance_seconds=getattr(settings, "HMAC_TOLERANCE_SECONDS", 300))
    if mode in {"oidc", "oauth2_oidc"}:
        lookup = oidc_token_lookup or getattr(settings, "OIDC_TOKEN_LOOKUP", None)
        if lookup is None:
            raise ValueError("oidc authentication requires oidc_token_lookup")
        return OIDCAuth(lookup)
    raise ValueError("Unknown AUTHENTICATION mode")


# Authentication's public API is gathered here so applications can discover
# every auth mechanism through ``lenapi.security``.  The higher-level factories
# and social login implementation live in focused modules; load them lazily to
# avoid a circular import (those modules use the primitives above).
_AUTHENTICATION_EXPORTS = {
    "UserRegistration",
    "UserPublic",
    "basic",
    "api_key",
    "jwt_auth",
    "session",
    "opaque_bearer",
    "hmac",
    "oidc",
    "webhook",
    "configure",
    "authentication_type",
    "authentication",
    "install_default_authentication",
    "register_user",
}
_SOCIAL_AUTH_EXPORTS = {"SocialAuth"}


def __getattr__(name: str):
    """Lazily expose all authentication APIs from this module."""
    if name in _AUTHENTICATION_EXPORTS:
        from importlib import import_module

        return getattr(import_module(".authentication", __package__), name)
    if name in _SOCIAL_AUTH_EXPORTS:
        from importlib import import_module

        return getattr(import_module(".auth", __package__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "JWTAuth", "APIKeyAuth", "BasicAuth", "CSRFProtect", "SessionAuth",
    "OpaqueBearerAuth", "HMACAuth", "OIDCAuth", "WebhookAuth",
    "hash_password", "verify_password", "make_password", "check_password",
    "make_api_key", "hash_api_key", "hmac_request_signature",
    "session_auth", "opaque_bearer_auth", "hmac_auth", "oidc_auth",
    "webhook_auth", "create_authenticator", *sorted(_AUTHENTICATION_EXPORTS),
    *sorted(_SOCIAL_AUTH_EXPORTS),
]
