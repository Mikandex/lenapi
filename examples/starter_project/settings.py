"""Project settings for lenApp."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
API_TITLE = "lenApp"
API_VERSION = "0.1.0"
DOCS_URL = "/docs/"
OPENAPI_URL = "/openapi.json"
# runserver uses these values; CLI flags override them.
HOST = os.getenv("LENAPI_HOST", "127.0.0.1")
PORT = int(os.getenv("LENAPI_PORT", "8009"))
DATABASE_URL = os.getenv("LENAPI_DATABASE_URL", f"sqlite:///{BASE_DIR / 'lenapi.db'}")
ASYNC_DATABASE_URL = os.getenv("LENAPI_ASYNC_DATABASE_URL")
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"
SECURITY_STORAGE = "database"
REDIS_URL = "redis://127.0.0.1:6379/0"
# Choose: "jwt", "api_key", or "basic_login" for route dependencies.
AUTHENTICATION = "jwt"
API_KEY = os.getenv("LENAPI_API_KEY", "development-only-change-me")
# Generated uniquely for this project. Keep it private; changing it invalidates tokens.
SECRET_KEY = os.getenv("LENAPI_SECRET_KEY", 'development-only-change-me-please-use-32-chars-minimum')
TOKEN = {
    "SECRET_KEY": SECRET_KEY,
    "LIFETIME_SECONDS": 3600,
    "REFRESH_LIFETIME_SECONDS": 2592000,
    "ISSUER": API_TITLE,
}
