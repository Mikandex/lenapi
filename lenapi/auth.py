"""Social sign-in route generation for lenApi applications."""

from __future__ import annotations

import base64
import hashlib
import importlib
import secrets
from urllib.parse import urlencode, urlsplit

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from .authentication import jwt_auth
from .concurrency import call_sync_or_async
from .models import SOCIAL_ACCOUNT_TABLE, User, _session
from .request import get_current_request
from .response import HTTPException, JSONResponse, Response


class SocialAuth:
    """Create OAuth login and callback routes for common social providers.

    Mount the returned router explicitly, for example::

        auth = SocialAuth(providers=["google", "github"], auto_register=True)
        app.include_router(auth.router(), prefix="/auth")

    Provider credentials and redirect URIs are read from the project's
    ``settings.py`` module unless a settings object is passed explicitly.
    """

    _PROVIDERS = {"google", "github", "linkedin"}
    _DISABLED_PROVIDERS = {"x", "facebook"}
    _ALIASES = {"twitter": "x"}

    def __init__(
        self,
        providers,
        *,
        auto_register: bool = False,
        jwt_enabled: bool = False,
        settings=None,
        user_model=None,
        allowed_origins=None,
    ) -> None:
        if isinstance(providers, str):
            raise TypeError("providers must be a list or tuple of provider names")
        try:
            requested = list(providers)
        except TypeError as exc:
            raise TypeError("providers must be a list or tuple of provider names") from exc
        normalized = []
        ignored_disabled_provider = False
        for value in requested:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("provider names must be non-empty strings")
            provider = self._ALIASES.get(value.strip().lower(), value.strip().lower())
            if provider in self._DISABLED_PROVIDERS:
                ignored_disabled_provider = True
                continue
            if provider not in self._PROVIDERS:
                supported = ", ".join(sorted(self._PROVIDERS))
                raise ValueError(f"Unsupported social provider '{value}'. Choose from: {supported}")
            if provider not in normalized:
                normalized.append(provider)
        if not normalized and not ignored_disabled_provider:
            raise ValueError("SocialAuth requires at least one provider")
        self.providers = tuple(normalized)
        self.auto_register = bool(auto_register)
        self.jwt_enabled = bool(jwt_enabled)
        self.settings = settings or self._load_settings()
        if allowed_origins is None:
            allowed_origins = getattr(self.settings, "SOCIAL_AUTH_ALLOWED_ORIGINS", ())
        self.allowed_origins = self._normalize_origins(allowed_origins)
        self.user_model = user_model or self._load_user_model()
        self.jwt = jwt_auth(self.settings) if self.jwt_enabled else None

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        if not isinstance(origin, str) or not origin.strip():
            raise ValueError("social auth origins must be non-empty origin URLs")
        parsed = urlsplit(origin.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"Invalid social auth origin: {origin!r}")
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        default_port = (parsed.scheme.lower() == "https" and port == 443) or (
            parsed.scheme.lower() == "http" and port == 80
        )
        authority = host if port is None or default_port else f"{host}:{port}"
        return f"{parsed.scheme.lower()}://{authority}"

    @classmethod
    def _normalize_origins(cls, origins) -> frozenset[str]:
        if isinstance(origins, str):
            raise TypeError("allowed_origins must be a list or tuple of origins")
        try:
            values = tuple(origins)
        except TypeError as exc:
            raise TypeError("allowed_origins must be a list or tuple of origins") from exc
        return frozenset(cls._normalize_origin(origin) for origin in values)

    def _check_login_origin(self) -> None:
        """Reject browser login requests whose supplied Origin is not trusted.

        Top-level GET navigations may omit Origin. Those requests remain guarded
        by the mandatory per-browser state cookie checked by the callback.
        """
        origin = get_current_request().headers.get("origin")
        if origin is None:
            return
        try:
            normalized = self._normalize_origin(origin)
        except ValueError:
            raise HTTPException(403, "Social login origin is not allowed") from None
        if not self.allowed_origins or normalized not in self.allowed_origins:
            raise HTTPException(403, "Social login origin is not allowed")

    @staticmethod
    def _load_settings():
        try:
            return importlib.import_module("settings")
        except ModuleNotFoundError as exc:
            if exc.name != "settings":
                raise
            raise ValueError(
                "SocialAuth needs the project's settings.py; pass settings= explicitly"
            ) from exc

    @staticmethod
    def _load_user_model():
        """Prefer an app's User subclass when it extends the framework model."""
        try:
            project_models = importlib.import_module("app.models")
        except ModuleNotFoundError as exc:
            if exc.name not in {"app", "app.models"}:
                raise
            return User
        candidate = getattr(project_models, "User", None)
        try:
            return candidate if isinstance(candidate, type) and issubclass(candidate, User) else User
        except TypeError:
            return User

    def router(self):
        """Build an unprefixed router; mount it with ``prefix="/auth"``."""
        from .application import Router

        router = Router(tag_name="Authentication")
        for provider in self.providers:
            self._add_provider_routes(router, provider)
        return router

    def _provider_config(self, provider: str) -> tuple[str, str, str, bool]:
        prefix = provider.upper()
        client_id = getattr(self.settings, f"{prefix}_CLIENT_ID", "")
        client_secret = getattr(self.settings, f"{prefix}_CLIENT_SECRET", "")
        redirect_uri = getattr(self.settings, f"{prefix}_REDIRECT_URI", "")
        cookie_secure = getattr(
            self.settings,
            f"{prefix}_COOKIE_SECURE",
            str(redirect_uri).lower().startswith("https://"),
        )
        if not client_id or not client_secret or not redirect_uri:
            raise HTTPException(
                503,
                f"{provider} social login needs {prefix}_CLIENT_ID, "
                f"{prefix}_CLIENT_SECRET, and {prefix}_REDIRECT_URI in settings.py",
            )
        return str(client_id), str(client_secret), str(redirect_uri), bool(cookie_secure)

    def _add_provider_routes(self, router, provider: str) -> None:
        state_cookie = f"lenapi_social_{provider}_state"
        verifier_cookie = f"lenapi_social_{provider}_verifier"
        use_pkce = provider == "google"

        @router.get(f"/{provider}/login")
        def login():
            self._check_login_origin()
            client_id, _client_secret, redirect_uri, cookie_secure = self._provider_config(provider)
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64) if use_pkce else None
            challenge = None
            if verifier:
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode("ascii")).digest()
                ).rstrip(b"=").decode("ascii")
            location = self._authorization_url(provider, client_id, redirect_uri, state, challenge)
            response = Response(status_code=302, headers={"location": location})
            cookie_options = {
                "max_age": 600,
                "path": "/",
                "secure": cookie_secure,
                "httponly": True,
                "samesite": "lax",
            }
            response.set_cookie(state_cookie, state, **cookie_options)
            if verifier:
                response.set_cookie(verifier_cookie, verifier, **cookie_options)
            return response
        login.__name__ = f"{provider}_login"

        @router.get(f"/{provider}/callback")
        async def callback():
            request = get_current_request()
            params = request.query_params
            state = params.get("state")
            expected_state = request.cookies.get(state_cookie)
            if not state or not expected_state or not secrets.compare_digest(state, expected_state):
                return self._callback_error(provider, state_cookie, verifier_cookie,
                                            "Invalid or expired OAuth state", 400)
            if params.get("error"):
                return self._callback_error(provider, state_cookie, verifier_cookie,
                                            f"{provider} sign-in was not completed", 400)
            code = params.get("code")
            if not code:
                return self._callback_error(provider, state_cookie, verifier_cookie,
                                            "Missing authorization code", 400)
            verifier = request.cookies.get(verifier_cookie) if use_pkce else None
            if use_pkce and not verifier:
                return self._callback_error(provider, state_cookie, verifier_cookie,
                                            "Missing or expired PKCE verifier", 400)
            try:
                profile = await self._fetch_identity(provider, code, verifier)
                identity = self._normalize_identity(provider, profile)
                if not identity.get("sub"):
                    raise ValueError(f"{provider} profile did not include an account ID")
                user_id = (
                    await call_sync_or_async(self._resolve_user, provider, identity)
                    if self.auto_register else None
                )
            except HTTPException as exc:
                return self._callback_error(provider, state_cookie, verifier_cookie,
                                            str(exc.detail), exc.status_code)
            except Exception as exc:
                try:
                    import httpx
                except ImportError:  # pragma: no cover - dependency declared by the package
                    httpx = None
                if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    message = f"{provider} token exchange or profile lookup failed"
                elif httpx is not None and isinstance(exc, httpx.RequestError):
                    status = 502
                    message = f"{provider} authentication service is unavailable"
                elif isinstance(exc, IntegrityError):
                    status = 409
                    message = "Could not create the social account; check for an existing username or email"
                elif isinstance(exc, ValueError):
                    status = 401
                    message = str(exc)
                else:
                    status = 502
                    message = f"{provider} authentication failed"
                return self._callback_error(provider, state_cookie, verifier_cookie, message, status)

            result = {"identity": identity}
            if user_id is not None:
                result["user_id"] = user_id
            if self.jwt_enabled:
                subject = str(user_id) if user_id is not None else f"{provider}:{identity['sub']}"
                claims = {
                    "auth_provider": provider,
                    "email": identity.get("email"),
                    "email_verified": identity.get("email_verified", False),
                    "name": identity.get("name"),
                    "username": identity.get("username"),
                }
                result.update({
                    "access_token": self.jwt.create_access_token(subject, claims=claims),
                    "token_type": "bearer",
                    "expires_in": self.jwt.token_lifetime_seconds,
                })
            response = JSONResponse(result)
            self._clear_cookies(response, state_cookie, verifier_cookie)
            return response
        callback.__name__ = f"{provider}_callback"

    def _authorization_url(self, provider, client_id, redirect_uri, state, challenge):
        if provider == "google":
            endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
            values = {"response_type": "code", "scope": "openid email profile"}
        elif provider == "github":
            endpoint = "https://github.com/login/oauth/authorize"
            values = {"scope": "read:user user:email"}
        else:  # LinkedIn OpenID Connect
            endpoint = "https://www.linkedin.com/oauth/v2/authorization"
            values = {"response_type": "code", "scope": "openid profile email"}
        values.update({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
        if provider == "google" and challenge:
            values.update({"code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{endpoint}?{urlencode(values)}"

    async def _fetch_identity(self, provider: str, code: str, verifier: str | None):
        import httpx

        client_id, client_secret, redirect_uri, _cookie_secure = self._provider_config(provider)
        async with httpx.AsyncClient(timeout=15.0) as client:
            if provider in {"google", "linkedin"}:
                token_endpoint = (
                    "https://oauth2.googleapis.com/token"
                    if provider == "google"
                    else "https://www.linkedin.com/oauth/v2/accessToken"
                )
                form = {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
                if verifier:
                    form["code_verifier"] = verifier
                response = await client.post(token_endpoint, data=form)
                response.raise_for_status()
                token = response.json().get("access_token")
                if not token:
                    raise ValueError(f"{provider} did not return an access token")
                userinfo_url = (
                    "https://openidconnect.googleapis.com/v1/userinfo"
                    if provider == "google"
                    else "https://api.linkedin.com/v2/userinfo"
                )
                profile_response = await client.get(
                    userinfo_url, headers={"Authorization": f"Bearer {token}"}
                )
                profile_response.raise_for_status()
                return profile_response.json()

            if provider == "github":
                response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    data={"client_id": client_id, "client_secret": client_secret,
                          "code": code, "redirect_uri": redirect_uri},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                token = response.json().get("access_token")
                if not token:
                    raise ValueError("GitHub did not return an access token")
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                profile_response = await client.get("https://api.github.com/user", headers=headers)
                profile_response.raise_for_status()
                profile = profile_response.json()
                emails_response = await client.get("https://api.github.com/user/emails", headers=headers)
                if emails_response.is_success:
                    verified_primary = next((item for item in emails_response.json()
                                             if item.get("primary") and item.get("verified")), None)
                    if verified_primary:
                        profile["email"] = verified_primary.get("email")
                        profile["email_verified"] = True
                return profile

            raise ValueError(f"Unsupported social provider '{provider}'")

    @staticmethod
    def _normalize_identity(provider: str, profile: dict) -> dict:
        if provider == "github":
            subject = profile.get("id")
            first_name = ""
            last_name = ""
            if profile.get("name"):
                names = str(profile["name"]).split(maxsplit=1)
                first_name = names[0]
                last_name = names[1] if len(names) > 1 else ""
            return {
                "provider": provider, "sub": str(subject) if subject is not None else None,
                "username": profile.get("login"), "name": profile.get("name"),
                "first_name": first_name, "last_name": last_name,
                "email": profile.get("email"), "email_verified": bool(profile.get("email_verified")),
                "picture": profile.get("avatar_url"),
            }
        subject = profile.get("sub") or profile.get("id")
        first_name = profile.get("given_name", "")
        last_name = profile.get("family_name", "")
        if not first_name and profile.get("name"):
            names = str(profile["name"]).split(maxsplit=1)
            first_name = names[0]
            last_name = last_name or (names[1] if len(names) > 1 else "")
        picture = profile.get("picture")
        return {
            "provider": provider, "sub": str(subject) if subject is not None else None,
            "username": profile.get("login"), "name": profile.get("name"),
            "first_name": first_name, "last_name": last_name,
            "email": profile.get("email"),
            "email_verified": bool(profile.get("email_verified", False)),
            "picture": picture,
        }

    def _resolve_user(self, provider: str, identity: dict) -> str:
        columns = self.user_model.__table__.columns
        if "username" not in columns or "password" not in columns:
            raise ValueError("auto_register needs a user model with username and password fields")
        session = _session()
        account_table = SOCIAL_ACCOUNT_TABLE
        account = session.execute(
            select(account_table.c.user_id).where(
                account_table.c.provider == provider,
                account_table.c.subject == identity["sub"],
            )
        ).first()
        if account is not None:
            return str(account.user_id)

        email = identity.get("email") if identity.get("email_verified") else None
        if email and "email" in columns:
            existing = self.user_model.get(email=email).get()
            if existing is not None:
                raise HTTPException(
                    409,
                    "This verified email already belongs to an account; sign in to that account and link the provider explicitly",
                )
        username = f"{provider}_{hashlib.sha256(identity['sub'].encode('utf-8')).hexdigest()}"
        values = {
            "username": username,
            "password": self._password_hash(),
        }
        if "first_name" in columns:
            values["first_name"] = identity.get("first_name") or ""
        if "last_name" in columns:
            values["last_name"] = identity.get("last_name") or ""
        if email and "email" in columns:
            values["email"] = email
        user = self.user_model.create(**values)
        try:
            session.execute(insert(account_table).values(
                provider=provider,
                subject=identity["sub"],
                user_id=user.id,
            ))
            session.commit()
        except IntegrityError:
            session.rollback()
            raise
        return str(user.id)

    @staticmethod
    def _password_hash():
        from .security import make_password

        return make_password(secrets.token_urlsafe(48))

    @staticmethod
    def _callback_error(provider, state_cookie, verifier_cookie, message, status):
        response = JSONResponse({"error": message}, status_code=status)
        SocialAuth._clear_cookies(response, state_cookie, verifier_cookie)
        return response

    @staticmethod
    def _clear_cookies(response, state_cookie, verifier_cookie):
        response.delete_cookie(state_cookie, path="/")
        response.delete_cookie(verifier_cookie, path="/")


__all__ = ["SocialAuth"]
