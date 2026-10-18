"""Application entry point."""

from lenapi import Depends, LenApi, check_password, create_authenticator, hash_api_key, make_api_key, make_password
from app import models  # Import models so migrations can discover them.
import settings
from pydantic import BaseModel, ConfigDict, Field, model_validator

app = LenApi()

if settings.AUTHENTICATION == "basic_login":
    identifier_field = getattr(settings, "BASIC_AUTH_FIELD", "username")
    if identifier_field not in models.User.__table__.columns:
        raise ValueError(f"BASIC_AUTH_FIELD '{identifier_field}' is not a field on User")


def authenticate_user(identifier, password):
    identifier_field = getattr(settings, "BASIC_AUTH_FIELD", "username")
    user = models.User.get(**{identifier_field: identifier}).get()
    if user is not None and check_password(password, user.password):
        return str(user.id)
    return None


def authenticate_api_key(api_key):
    if not api_key:
        return None
    user = models.User.get(api_key=hash_api_key(api_key)).get()
    return {"user_id": str(user.id), "auth_method": "api_key"} if user is not None else None


authenticator = create_authenticator(
    settings, authenticate=authenticate_user, authenticate_api_key=authenticate_api_key
)
if settings.AUTHENTICATION == "jwt":
    app.include_router(authenticator.router(authenticate_user))


class UserRegistration(BaseModel):
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
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    first_name: str
    last_name: str
    api_key: str | None = None


@app.post("/auth/register", response_model=UserPublic)
def register_user(data: UserRegistration):
    api_key = make_api_key() if settings.AUTHENTICATION == "api_key" else None
    user = models.User.create(
        username=data.username,
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        password=make_password(data.password),
        api_key=hash_api_key(api_key) if api_key is not None else None,
    )
    return {"id": user.id, "username": user.username, "email": user.email,
            "first_name": user.first_name, "last_name": user.last_name, "api_key": api_key}


@app.get("/auth/profile")
def auth_profile(identity=Depends(authenticator)):
    if isinstance(identity, dict) and "sub" in identity:
        return {"user_id": identity["sub"]}
    return {"identity": identity}


@app.get("/")
def home():
    return {"message": "lenApp is running"}
