"""Application-level authentication helpers for lenApi projects.

Use these factories to connect lenApi's authentication dependencies to a user
model, rather than putting credential lookup and password handling in
``main.py``.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .security import (
    APIKeyAuth,
    BasicAuth,
    HMACAuth,
    JWTAuth,
    OIDCAuth,
    OpaqueBearerAuth,
    SessionAuth,
    WebhookAuth,
    check_password,
    hash_api_key,
    make_api_key,
    make_password,
)


class UserRegistration(BaseModel):
    """Default request schema for registering a project user."""

    username: str = Field(min_length=1, max_length=120)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    first_name: str = Field(default="", max_length=150)
    last_name: str = Field(default="", max_length=150)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.password != self.confirm_password:
            raise ValueError("password and confirm_password must match")
        return self


class UserPublic(BaseModel):
    """Default public representation returned after user registration."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    first_name: str
    last_name: str
    api_key: str | None = None


def _user_authenticator(user_model, identifier_field="username", password_field="password"):
    columns = user_model.__table__.columns
    if identifier_field not in columns:
        raise ValueError(f"Authentication identifier '{identifier_field}' is not a field on {user_model.__name__}")
    if password_field not in columns:
        raise ValueError(f"Password field '{password_field}' is not a field on {user_model.__name__}")

    def authenticate(identifier, password):
        user = user_model.get(**{identifier_field: identifier}).get()
        if user is not None and check_password(password, getattr(user, password_field)):
            return str(user.id)
        return None

    return authenticate


def basic(user_model, *, identifier_field="username", password_field="password"):
    """Create database-backed HTTP Basic authentication for a user model."""
    return BasicAuth(_user_authenticator(user_model, identifier_field, password_field))


def api_key(user_model, *, field="api_key", header_name="x-api-key"):
    """Create database-backed API key authentication for a user model.

    Raw API keys are hashed before lookup. Store only ``hash_api_key(key)`` in
    the configured model field.
    """
    if field not in user_model.__table__.columns:
        raise ValueError(f"API key field '{field}' is not a field on {user_model.__name__}")

    def authenticate(raw_key):
        if not raw_key:
            return None
        user = user_model.get(**{field: hash_api_key(raw_key)}).get()
        return {"user_id": str(user.id), "auth_method": "api_key"} if user is not None else None

    return APIKeyAuth(header_name=header_name, authenticate=authenticate)


def jwt_auth(settings, user_model=None, *, identifier_field="username", password_field="password"):
    """Create JWT authentication, optionally backed by a project user model.

    Use ``auth.router(authenticate)`` to mount token and refresh endpoints.
    """
    token = settings.TOKEN
    auth = JWTAuth(
        token["SECRET_KEY"],
        token_lifetime_seconds=token.get("LIFETIME_SECONDS", 3600),
        refresh_token_lifetime_seconds=token.get("REFRESH_LIFETIME_SECONDS", 60 * 60 * 24 * 30),
        issuer=token.get("ISSUER"),
        authenticate=(
            _user_authenticator(user_model, identifier_field, password_field)
            if user_model is not None else None
        ),
    )
    return auth


def session(*, store=None, lookup=None, cookie_name="lenapi_session"):
    """Create session authentication for use as a route dependency."""
    return SessionAuth(lookup, store=store if lookup is None else None, cookie_name=cookie_name)


def opaque_bearer(*, store=None, lookup=None):
    """Create server-checked opaque bearer token authentication."""
    return OpaqueBearerAuth(lookup, store=store if lookup is None else None)


def hmac(secret, *, tolerance_seconds=300, nonce_store=None):
    """Create optional HMAC request authentication."""
    return HMACAuth(secret, tolerance_seconds=tolerance_seconds, nonce_store=nonce_store)


def oidc(lookup):
    """Create OIDC authentication backed by a token lookup callback."""
    return OIDCAuth(lookup)


def webhook(secret, *, tolerance_seconds=300, nonce_store=None):
    """Create webhook signature authentication."""
    return WebhookAuth(secret, tolerance_seconds=tolerance_seconds, nonce_store=nonce_store)


def configure(settings, user_model, *, security_store=None):
    """Build the primary authenticator selected by ``settings.AUTHENTICATION``."""
    mode = getattr(settings, "AUTHENTICATION", "jwt").lower()
    identifier_field = getattr(settings, "BASIC_AUTH_FIELD", "username")
    if mode == "basic_login":
        return basic(user_model, identifier_field=identifier_field)
    if mode == "api_key":
        return api_key(user_model)
    if mode == "jwt":
        return jwt_auth(settings, user_model)
    if mode == "session":
        options = getattr(settings, "SESSION", {})
        return session(store=security_store, cookie_name=options.get("COOKIE_NAME", "lenapi_session"))
    if mode == "opaque_bearer":
        return opaque_bearer(store=security_store)
    if mode == "hmac":
        secret = getattr(settings, "HMAC_SECRET", None)
        if not secret:
            raise ValueError("hmac authentication requires HMAC_SECRET")
        return hmac(secret, tolerance_seconds=getattr(settings, "HMAC_TOLERANCE_SECONDS", 300))
    if mode in {"oidc", "oauth2_oidc"}:
        lookup = getattr(settings, "OIDC_TOKEN_LOOKUP", None)
        if lookup is None:
            raise ValueError("oidc authentication requires OIDC_TOKEN_LOOKUP")
        return oidc(lookup)
    raise ValueError(f"Unknown AUTHENTICATION mode: {mode}")


def authentication_type(auth_type, *, settings, user_model, app):
    """Configure an application's selected authentication method in one call.

    ``auth_type`` may be ``"jwt"``, ``"api_key"``, ``"basic_login``,
    ``"session"``, ``"opaque_bearer"``, ``"hmac"``, or ``"oidc"``. The
    returned object is the dependency to use with ``Depends``. JWT token and
    refresh routes are added to ``app`` automatically.
    """
    from types import SimpleNamespace

    mode = str(auth_type).lower()
    selected_settings = SimpleNamespace(**{
        key: value for key, value in vars(settings).items() if not key.startswith("__")
    })
    selected_settings.AUTHENTICATION = mode
    authenticator = configure(
        selected_settings,
        user_model,
        security_store=getattr(app, "security_store", None),
    )
    if mode == "jwt":
        app.include_router(authenticator.router())
    return authenticator


def register_user(user_model, data, *, authentication="jwt"):
    """Create a user, hash its password, and return a one-time API key if used."""
    raw_key = make_api_key() if authentication == "api_key" else None
    user = user_model.create(
        username=data.username,
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        password=make_password(data.password),
        api_key=hash_api_key(raw_key) if raw_key is not None else None,
    )
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "api_key": raw_key,
    }


__all__ = [
    "UserRegistration", "UserPublic", "basic", "api_key", "jwt_auth",
    "session", "opaque_bearer", "hmac", "oidc", "webhook", "configure",
    "authentication_type",
    "register_user",
]
