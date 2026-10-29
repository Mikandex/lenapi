"""Application-level authentication helpers for lenApi projects.

Use these factories to connect lenApi's authentication dependencies to a user
model, rather than putting credential lookup and password handling in
``main.py``.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from importlib import import_module

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

# A fixed, valid hash keeps the missing-user path doing password-hash work too,
# reducing username-enumeration timing differences. This value is never used
# to authenticate an account.
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$1800000$Pe8aASgaamrPqx7hXk1orO$"
    "9esnpuqwJ8N1RJhCnLIYcGD9ymQibrGYW6gbvheDbiw="
)


class UserRegistration(BaseModel):
    """Default request schema for registering a project user."""

    username: str = Field(min_length=1, max_length=120)
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
        stored_password = (
            getattr(user, password_field) if user is not None else _DUMMY_PASSWORD_HASH
        )
        password_matches = check_password(password, stored_password)
        if user is not None and password_matches:
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


def jwt_auth(settings, user_model=None, *, identifier_field="username", password_field="password",
             security_store=None):
    """Create JWT authentication, optionally backed by a project user model.

    Use ``auth.router(authenticate)`` to mount token and refresh endpoints.
    """
    token = settings.TOKEN
    auth = JWTAuth(
        token["SECRET_KEY"],
        token_lifetime_seconds=token.get("LIFETIME_SECONDS", 3600),
        refresh_token_lifetime_seconds=token.get("REFRESH_LIFETIME_SECONDS", 60 * 60 * 24 * 30),
        issuer=token.get("ISSUER"),
        security_store=security_store,
        login_field=identifier_field,
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


def configure(
    settings,
    user_model=None,
    *,
    security_store=None,
    api_key_field=None,
    identifier_field=None,
    password_field="password",
):
    """Build the primary authenticator selected by ``settings.AUTHENTICATION``."""
    if user_model is None:
        from .models import User

        user_model = User
    mode = getattr(settings, "AUTHENTICATION", "jwt").lower()
    identifier_field = identifier_field or getattr(settings, "BASIC_AUTH_FIELD", "username")
    if mode == "basic_login":
        return basic(
            user_model,
            identifier_field=identifier_field,
            password_field=password_field,
        )
    if mode == "api_key":
        return api_key(user_model, field=api_key_field or "api_key")
    if mode == "jwt":
        return jwt_auth(
            settings,
            user_model,
            identifier_field=identifier_field,
            password_field=password_field,
            security_store=security_store,
        )
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


def authentication_type(
    auth_type,
    *,
    settings,
    user_model=None,
    api_key_field=None,
    identifier_field=None,
    password_field="password",
    app=None,
):
    """Create the authenticator selected by a project settings module.

    ``auth_type`` may be ``"jwt"``, ``"api_key"``, ``"basic_login``,
    ``"session"``, ``"opaque_bearer"``, ``"hmac"``, or ``"oidc"``. The
    returned object is the dependency to use with ``Depends``. This only
    configures authentication; it does not add login, registration, refresh,
    or profile routes. If ``user_model`` is omitted, the framework's default
    ``User`` model is used.
    """
    from types import SimpleNamespace

    mode = str(auth_type).lower()
    if mode == "api_key" and user_model is not None and not api_key_field:
        raise ValueError(
            "Custom API-key authentication requires api_key_field matching a column "
            f"on {user_model.__name__}; for example, api_key_field='key_digest'."
        )
    selected_settings = SimpleNamespace(**{
        key: value for key, value in vars(settings).items() if not key.startswith("__")
    })
    selected_settings.AUTHENTICATION = mode
    authenticator = configure(
        selected_settings,
        user_model,
        security_store=getattr(app, "security_store", None),
        api_key_field=api_key_field,
        identifier_field=identifier_field,
        password_field=password_field,
    )
    return authenticator


def authentication(
    type="jwt",
    *,
    settings=None,
    user_model=None,
    api_key_field=None,
    identifier_field=None,
    password_field="password",
    app=None,
):
    """Create a route authentication dependency from project settings.

    By default, ``settings`` is imported from the current project and the
    framework's built-in ``User`` model is used. ``identifier_field`` selects
    the database field used for JWT/Basic login; for JWT it also names the
    identifier property in the token endpoint's JSON body. Pass the actual
    LENAPI instance as ``app`` to share its security store.
    """
    if settings is None:
        try:
            settings = import_module("settings")
        except ModuleNotFoundError as exc:
            if exc.name != "settings":
                raise
            raise ValueError(
                "authentication() needs a project settings.py; pass settings= explicitly"
            ) from exc
    return authentication_type(
        type,
        settings=settings,
        user_model=user_model,
        api_key_field=api_key_field,
        identifier_field=identifier_field,
        password_field=password_field,
        app=app,
    )


def install_default_authentication(app, settings):
    """Configure the app's authentication dependency without adding routes."""
    from .models import User

    mode = getattr(settings, "AUTHENTICATION", "jwt").lower()
    authenticator = authentication_type(
        mode, settings=settings, user_model=User,
        api_key_field="api_key" if mode == "api_key" else None,
        app=app,
    )

    app.authenticator = authenticator
    return authenticator


def register_user(user_model=None, data=None, *, authentication="jwt", api_key_field=None):
    """Create a user, hash its password, and return a one-time API key if used.

    Apps may define a custom registration schema with an ``email`` field. When
    provided, that value is passed through to models that expose an email column.
    """
    using_default_model = user_model is None
    if user_model is None:
        from .models import User

        user_model = User
    if authentication == "api_key":
        if using_default_model:
            api_key_field = api_key_field or "api_key"
        elif not api_key_field:
            raise ValueError(
                "Custom API-key registration requires api_key_field matching a column "
                f"on {user_model.__name__}; for example, api_key_field='key_digest'."
            )
        if api_key_field not in user_model.__table__.columns:
            raise ValueError(
                f"API-key field '{api_key_field}' is not a field on {user_model.__name__}."
            )
    raw_key = make_api_key() if authentication == "api_key" else None
    values = dict(
        username=data.username,
        first_name=data.first_name,
        last_name=data.last_name,
        password=make_password(data.password),
    )
    if raw_key is not None:
        values[api_key_field] = hash_api_key(raw_key)
    email = getattr(data, "email", None)
    if email is not None and "email" in user_model.__table__.columns:
        values["email"] = email
    user = user_model.create(**values)
    result = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "api_key": raw_key,
    }
    if email is not None and "email" in user_model.__table__.columns:
        result["email"] = user.email
    return result


__all__ = [
    "UserRegistration", "UserPublic", "basic", "api_key", "jwt_auth",
    "session", "opaque_bearer", "hmac", "oidc", "webhook", "configure",
    "authentication_type",
    "authentication",
    "install_default_authentication",
    "register_user",
]
