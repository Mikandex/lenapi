"""Project settings for the root lenApi example app."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
API_TITLE = "Ifeanyi"
API_VERSION = "0.1.0"
DOCS_URL = "/docs/"
OPENAPI_URL = "/openapi.json"
HOST = os.getenv("LENAPI_HOST", "127.0.0.1")
PORT = int(os.getenv("LENAPI_PORT", "8009"))
DATABASE_URL = os.getenv("LENAPI_DATABASE_URL", f"sqlite:///{BASE_DIR / 'lenapi.db'}")
# Use the same SQLite file for async queries by default. Migrations continue to
# use DATABASE_URL; other database engines can set LENAPI_ASYNC_DATABASE_URL.
ASYNC_DATABASE_URL = os.getenv("LENAPI_ASYNC_DATABASE_URL")
if ASYNC_DATABASE_URL is None and DATABASE_URL.startswith("sqlite:///"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"
SECURITY_STORAGE = "database"
CACHE_BACKEND = os.getenv("LENAPI_CACHE_BACKEND", "memory")  # memory, database, or redis
REDIS_URL = "redis://127.0.0.1:6379/0"
AUTHENTICATION = "jwt"  # jwt, api_key, basic_login, session, opaque_bearer,
BASIC_AUTH_FIELD = "username"  # For basic_login: username, email, phone, or another User field.
API_KEY = os.getenv("LENAPI_API_KEY", "development-only-change-me")
SECRET_KEY = os.getenv("LENAPI_SECRET_KEY", 'development-only-change-me-please-use-32-chars-minimum')
TOKEN = {
    "SECRET_KEY": SECRET_KEY,
    "LIFETIME_SECONDS": 3600,
    "REFRESH_LIFETIME_SECONDS": 2592000,
    "ISSUER": API_TITLE,
}
