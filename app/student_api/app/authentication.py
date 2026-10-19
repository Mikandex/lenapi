"""Authentication setup and endpoints for the example application."""

from lenapi import Depends, LenApi
from lenapi.authentication import (
    UserPublic,
    UserRegistration,
    authentication_type,
    register_user,
)

from app.models import User
import settings


app = LenApi()
auth_type = settings.AUTHENTICATION  # e.g. "jwt", "api_key", "basic_login", or "session"
authenticator = authentication_type(auth_type, settings=settings, user_model=User, app=app)


@app.post("/auth/register", response_model=UserPublic)
def create_user(data: UserRegistration):
    return register_user(User, data, authentication=auth_type)


@app.get("/auth/profile")
def auth_profile(identity=Depends(authenticator)):
    if isinstance(identity, dict) and "sub" in identity:
        return {"user_id": identity["sub"]}
    return {"identity": identity}
