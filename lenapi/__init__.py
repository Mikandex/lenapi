"""lenApi: a small, typed ASGI web framework."""

import sys

if sys.version_info < (3, 11):
    raise RuntimeError(
        "LenAPI requires Python 3.11 or newer. Python 3.10 reached end of life "
        "on 2026-10-01 and is no longer supported."
    )

__version__ = "0.1.4"

from .application import Application, LENAPI, Router
from .background import BackgroundQueue, BackgroundTasks
from .dependencies import Depends
from .authentication import authentication
from .auth import SocialAuth
from .authorization import require_owner, require_permission, require_role
from .database import AsyncDatabase, Database
from .transaction import transaction
from .mongodb import MongoDatabase, MongoDocument, MultipleDocumentsFound
from .parameters import Body, File
from .logging import get_logger, logger
from .models import (
    AutoField,
    Avg,
    AdminUser,
    BigIntField,
    BigIntegerField,
    BooleanField,
    CASCADE,
    Count,
    DateTimeField,
    DateField,
    DecimalField,
    DurationField,
    EmailField,
    FloatField,
    ForeignKeyField,
    FileField,
    FileImageField,
    ImageField,
    IPField,
    IntegerField,
    PositiveBigIntegerField,
    PositiveIntegerField,
    JSONField,
    ManyToManyField,
    OneToOneField,
    Max,
    Min,
    Model,
    PhoneField,
    PROTECT,
    SET_NULL,
    StringField,
    Sum,
    TextField,
    TimeField,
    URLField,
    UUIDField,
    User,
    cascade,
    null,
    protected,
)
from .request import Request, UploadFile
from .middleware import CORSMiddleware, DocsAuthMiddleware, GZipMiddleware, RequestIDMiddleware, TimingMiddleware
from .service import Cache, Faker, Ollama, OpenAI, WebSocket, WebSocketConnection, WebSocketDisconnect, WebSocketService
from .response import HTTPException, JSONResponse, Response
from .sentinels import UNSET
from .security import (
    APIKeyAuth,
    BasicAuth,
    CSRFProtect,
    HMACAuth,
    JWTAuth,
    OIDCAuth,
    OpaqueBearerAuth,
    SessionAuth,
    WebhookAuth,
    check_password,
    create_authenticator,
    hash_api_key,
    hmac_request_signature,
    hmac_auth,
    hash_password,
    make_api_key,
    make_password,
    oidc_auth,
    opaque_bearer_auth,
    session_auth,
    webhook_auth,
    verify_password,
)
from . import model

__all__ = [
    "Application", "LENAPI", "Router", "Depends", "BackgroundTasks", "BackgroundQueue", "Cache", "Faker", "OpenAI", "Ollama", "CORSMiddleware", "DocsAuthMiddleware", "GZipMiddleware", "RequestIDMiddleware", "TimingMiddleware", "require_role", "require_permission", "require_owner", "Database", "AsyncDatabase", "transaction", "MongoDatabase", "MongoDocument", "MultipleDocumentsFound", "Body", "File", "Model", "AdminUser", "AutoField", "logger", "get_logger", "WebSocket", "WebSocketConnection", "WebSocketDisconnect", "WebSocketService",
    "IntegerField", "PositiveIntegerField", "BigIntField", "BigIntegerField", "PositiveBigIntegerField", "DecimalField", "StringField", "EmailField", "IPField", "PhoneField", "TextField", "UUIDField", "ImageField", "FileField", "FileImageField", "URLField", "BooleanField", "FloatField", "User",
    "DateTimeField", "DateField", "TimeField", "DurationField", "JSONField", "ForeignKeyField", "OneToOneField", "ManyToManyField", "Count", "Sum", "Min", "Max", "Avg", "Request", "UploadFile", "Response", "JSONResponse",
    "HTTPException", "JWTAuth", "APIKeyAuth", "BasicAuth", "CSRFProtect", "SessionAuth", "OpaqueBearerAuth", "HMACAuth", "OIDCAuth", "WebhookAuth", "hmac_request_signature", "session_auth", "opaque_bearer_auth", "hmac_auth", "oidc_auth", "webhook_auth", "create_authenticator", "authentication", "SocialAuth", "hash_password", "verify_password", "make_password", "check_password", "make_api_key", "hash_api_key", "UNSET", "model", "CASCADE", "SET_NULL", "PROTECT", "cascade", "null", "protected",
]
