"""Command line interface for lenApi projects."""

import argparse
import csv
from importlib import import_module
from importlib.util import find_spec
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from . import __version__
from .logging import logger


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8009
DEFAULT_APP = "main:app"


PROJECT_FILES = {
    "__init__.py": "",
    ".gitignore": "__pycache__/\n.pytest_cache/\n*.py[cod]\n.env\nlenapi.db\nmedia/\nprivate_media/\n.venv/\nbuild/\ndist/\n*.egg-info/\n",
    "settings.py": '''"""Project settings for lenApp."""

import os
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
API_TITLE = "lenApp"
API_VERSION = "0.1.0"
DOCS_URL = "/docs/"
OPENAPI_URL = "/openapi.json"
SAFE_GUARD_DOCS_URL = False  # Set True to require an admin login for docs and OpenAPI.
DOCS_SESSION_LIFETIME_SECONDS = 3600
DOCS_COOKIE_SECURE = None  # None follows the ASGI HTTPS scheme; set True behind a TLS proxy.
DOCS_LOGIN_MAX_ATTEMPTS = 5  # Failed docs sign-ins allowed per client IP in the time window.
DOCS_LOGIN_WINDOW_SECONDS = 300
# Limit incoming request bodies to 10 MiB; set to None to disable.
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024
# Maximum time allowed to receive an incoming body; set to None to disable.
REQUEST_BODY_TIMEOUT_SECONDS = 30
# Shared storage for IP flags, sessions, opaque tokens, and HMAC replay checks.
# Choose "database" (requires migration), "redis", or "memory" (development only).
# Database mode uses the selected SQL database or MongoDB backend.
SECURITY_STORAGE = "database"
REDIS_URL = "redis://127.0.0.1:6379/0"
CACHE_BACKEND = "memory"  # "memory", "database", or "redis".
# Optional fixed-window request quotas. Scopes: "ip", "anonymous", "authenticated".
THROTTLE_RATES = {}  # Example: {"anonymous": "60/minute", "authenticated": "300/minute"}
# run uses these values; blank HOST falls back to 127.0.0.1.
HOST = ""
PORT = 8009
WEBSOCKET_ALLOWED_ORIGINS = []
DATABASE_TYPE = "sqlite"  # "sqlite", "mysql", "postgresql", or "mongodb".
DATABASE_URL = f"sqlite:///{BASE_DIR / 'lenapi.db'}"
ACTIVATE_ASYNC = True  # Async access uses the same database and URL as sync access.
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"
# Files here are not exposed by the built-in /media/ static route.
PRIVATE_MEDIA_ROOT = BASE_DIR / "private_media"
# Choose one primary method: "jwt", "api_key", "basic_login", "opaque_bearer", or "session".
# Session can also be added to selected routes; add HMAC/OIDC with Depends(...).
AUTHENTICATION = "jwt"
BASIC_AUTH_FIELD = "username"  # Basic login field on User: username, email, phone, etc.
API_KEY = "__LENAPI_GENERATED_API_KEY__"
# Optional Google OpenID Connect settings. Leave blank to disable Google login.
# Developers may set values here or load them from their preferred env manager.
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
GOOGLE_REDIRECT_URI = ""
GOOGLE_COOKIE_SECURE = urlparse(GOOGLE_REDIRECT_URI).scheme == "https"
GITHUB_CLIENT_ID = ""
GITHUB_CLIENT_SECRET = ""
GITHUB_REDIRECT_URI = ""
GITHUB_COOKIE_SECURE = urlparse(GITHUB_REDIRECT_URI).scheme == "https"
LINKEDIN_CLIENT_ID = ""
LINKEDIN_CLIENT_SECRET = ""
LINKEDIN_REDIRECT_URI = ""
LINKEDIN_COOKIE_SECURE = urlparse(LINKEDIN_REDIRECT_URI).scheme == "https"
# Exact browser origins allowed to start social login; configure your frontend origin.
SOCIAL_AUTH_ALLOWED_ORIGINS = []
# Keep this generated key private. Rotate it to invalidate existing tokens.
SECRET_KEY = "__LENAPI_GENERATED_SECRET_KEY__"
TOKEN = {
    "SECRET_KEY": SECRET_KEY,
    "LIFETIME_SECONDS": 3600,
    "REFRESH_LIFETIME_SECONDS": 2592000,
    "ISSUER": API_TITLE,
}
''',
    "main.py": '''"""Application routes."""

from lenapi import LENAPI
from app import models  # Import models so migrations can discover them.

app = LENAPI()


@app.get("/")
def home():
    return {"message": "lenApp is running"}
''',
    "app/__init__.py": '''"""Application package."""
''',
    "app/models.py": '''"""Define project-specific database models here.

The framework-provided User model is included in the first migration
automatically. Subclass it here to add columns to the same user table; the
migration command detects those added columns.
"""

# Uncomment this example to extend the framework User table:
# from sqlalchemy import false
#
# from lenapi import model
# from lenapi.models import User as BaseUser
#
# class User(BaseUser):
#     bio = model.TextField(default="", nullable=True)
#     is_verified = model.BooleanField(
#         default=False, nullable=False, server_default=false()
#     )

# Define other project models below as needed.
''',
    "app/schemas.py": '''"""Project request and response schemas."""

# Default account schemas are supplied by the framework.
from lenapi.authentication import UserPublic, UserRegistration
''',
    "lenora.py": '''"""Project-local command launcher."""

import sys
from pathlib import Path

project_dir = Path(__file__).resolve().parent
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != project_dir]
sys.path.insert(0, str(project_dir.parent))
from lenapi.cli import main
sys.path.insert(0, str(project_dir))


if __name__ == "__main__":
    main()
''',
    "alembic.ini": '''[alembic]
script_location = %(here)s/migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///lenapi.db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers = console
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
''',
    "migrations/env.py": '''"""Alembic environment for lenApp models."""

from importlib import import_module

from alembic import context
from sqlalchemy import create_engine, inspect

from lenapi import Model

config = context.config


def load_app():
    app_path = config.attributes.get("app_import_string", "main:app")
    module_name, _, attribute_path = app_path.partition(":")
    try:
        import_module("app.models")
    except ModuleNotFoundError as exc:
        if exc.name not in {"app", "app.models"}:
            raise
    app = import_module(module_name)
    for name in attribute_path.split(".") if attribute_path else []:
        app = getattr(app, name)
    return app


def run_migrations_offline():
    app = load_app()
    context.configure(
        url=app.database.engine.url.render_as_string(hide_password=False),
        target_metadata=Model.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=lambda obj, name, type_, reflected, compare_to: (
            config.attributes.get("lenapi_include_background_jobs", False)
            if type_ == "table" and name == "lenapi_background_jobs"
            else True
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    app = load_app()
    engine = app.database.engine

    def configure_and_run(connection):
        context.configure(
            connection=connection,
            target_metadata=Model.metadata,
            compare_type=True,
            include_object=lambda obj, name, type_, reflected, compare_to: (
                config.attributes.get("lenapi_include_background_jobs", False)
                if type_ == "table" and name == "lenapi_background_jobs"
                else True
            ),
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()

    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        configure_and_run(supplied_connection)
        return

    with engine.connect() as connection:
        existing_tables = set(inspect(connection).get_table_names()) - {"alembic_version"}
        if connection.in_transaction():
            connection.commit()
        if (config.attributes.get("lenapi_initial_revision", False) and existing_tables
                and connection.dialect.name == "sqlite"):
            with create_engine("sqlite://").connect() as empty_connection:
                configure_and_run(empty_connection)
        else:
            configure_and_run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
''',
    "migrations/script.py.mako": '''"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
''',
    "migrations/versions/__init__.py": "",
    "migrations/versions/.gitkeep": "",
    "README.md": '''# lenApp

Project settings are in `settings.py`; project models and schemas belong in
`app/models.py` and `app/schemas.py`, and routes belong in `main.py`. The
framework supplies the default `User` model and authentication setup. The
first migration includes the framework `User` and `AdminUser` tables.

```bash
python lenora.py migration
python lenora.py run
```

Migration checks for schema changes and skips the database upgrade when there is no new revision to apply. Set `SAFE_GUARD_DOCS_URL = True` in `settings.py` and run `python lenora.py admin` to enable database-backed login for `/docs/`.

The development server uses port 8009. Open `/docs/` for Swagger UI. Run `python lenora.py test` to generate endpoint checks, run the tests, and write a CSV report.
''',
    "tests/__init__.py": '''"""Project test package. Run its tests with ``python lenora.py test``."""
''',
    "pytest.ini": '''[pytest]
python_files = test_*.py *_test.py
''',
}


def _run_registered_background_worker(module_name: str, app) -> None:
    """Run a queue exported by main.py after its storage migration is applied."""
    module = import_module(module_name)
    queue = (
        getattr(module, "background_queue", None)
        or getattr(module, "queue", None)
        or getattr(app, "background_queue", None)
        or getattr(app, "queue", None)
    )
    if queue is None:
        logger.info(
            "Background queue storage is ready. Export `queue` or `background_queue` from the app module to run handlers."
        )
        return
    if not hasattr(queue, "run_worker"):
        raise TypeError("the app's queue/background_queue must be a lenApi BackgroundQueue")
    import asyncio
    asyncio.run(queue.run_worker())


def _main() -> None:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="lenApi project tools",
        epilog=(
            "Examples:\n"
            "  python lenora.py startproject myproject\n"
            "  python lenora.py apikey\n"
            "  python lenora.py run --port 8010\n"
            "  python lenora.py migration --message add_users\n\n"
            "For flags and arguments for a command, run: "
            "python lenora.py <command> --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run", aliases=["runserver"], help="run the ASGI development server")
    run_command.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    run_command.add_argument("--host", help="host to bind (default: project settings or 127.0.0.1)")
    run_command.add_argument("--port", type=int, help="port to bind (default: project settings or 8009)")
    reload_options = run_command.add_mutually_exclusive_group()
    reload_options.add_argument("--reload", dest="reload", action="store_true", help="reload when project files change (default)")
    reload_options.add_argument("--no-reload", dest="reload", action="store_false", help="disable automatic reload")
    run_command.set_defaults(reload=True)

    startproject = commands.add_parser("startproject", help="create a new lenApi project")
    startproject.add_argument("name", nargs="?", default="lenApp", help="project folder name (default: lenApp)")

    commands.add_parser(
        "repair-settings",
        help="recreate the project settings.py only when it is missing",
    )

    apikey_command = commands.add_parser(
        "apikey", help="enable API-key authentication, add its model, and migrate"
    )
    apikey_command.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")

    migration = commands.add_parser("migration", help="generate and apply a migration from model changes")
    migration.add_argument("--message", "-m", default="schema update", help="migration description")
    migration.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    migrate = commands.add_parser("migrate", help="apply pending migrations to the database")
    migrate.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    background_worker = commands.add_parser(
        "background-worker", help="migrate durable background queue storage and run a registered worker"
    )
    background_worker.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    background_worker.add_argument("--message", "-m", default="activate background worker")
    showmigrations = commands.add_parser("showmigrations", help="show local and applied database migration history")
    showmigrations.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    repair_migrations = commands.add_parser(
        "repair-migrations", help="repair a missing database revision when the schema matches current models"
    )
    repair_migrations.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    clear_migrations = commands.add_parser(
        "clear_migrations", help="clear migration files and version history for an empty database only"
    )
    clear_migrations.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    delete_migrations = commands.add_parser(
        "delete_migrations", help="confirm and delete all database tables and local migration revisions"
    )
    delete_migrations.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    admin_command = commands.add_parser(
        "admin", help="create an administrator account for private Swagger documentation"
    )
    admin_command.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")

    generate_test = commands.add_parser("generate_test", help="generate pytest endpoint coverage from the app routes")
    generate_test.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    run_test = commands.add_parser("run_test", help="run pytest and list the API endpoints covered by generated tests")
    run_test.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    run_test.add_argument("paths", nargs="*", help="optional pytest files or directories")
    run_test_output = commands.add_parser(
        "run_test_output", help="run pytest in the terminal and write a CSV test report"
    )
    run_test_output.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    run_test_output.add_argument(
        "--output", default="outputs/lenapi_pytest_results.csv",
        help="CSV report path (default: outputs/lenapi_pytest_results.csv)",
    )
    run_test_output.add_argument("paths", nargs="*", help="optional pytest files or directories")
    test_command = commands.add_parser(
        "test", help="generate endpoint checks, run tests, and write a CSV report"
    )
    test_command.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    test_command.add_argument(
        "--output", default="outputs/lenapi_test_results.csv",
        help="CSV report path (default: outputs/lenapi_test_results.csv)",
    )
    test_command.add_argument("paths", nargs="*", help="optional pytest files or directories")

    args = parser.parse_args()
    # Keep the former spelling working for existing projects and scripts.
    if args.command == "runserver":
        args.command = "run"

    if args.command in {"run_test", "run_test_output", "test"} and find_spec("pytest") is None:
        parser.error("pytest is missing; reinstall lenApi or install it with `python -m pip install pytest`")

    if args.command == "run":
        project_settings = None
        if (Path.cwd() / "settings.py").is_file():
            project_settings = import_module("settings")
        args.host = args.host or getattr(project_settings, "HOST", "") or DEFAULT_HOST
        args.port = args.port or int(getattr(project_settings, "PORT", DEFAULT_PORT))

    if args.command == "startproject":
        if not args.name.isidentifier():
            parser.error("project name must be a valid Python package name")
        project_dir = Path.cwd() / args.name
        if project_dir.exists():
            parser.error(f"{project_dir} already exists")
        try:
            for relative_path, contents in PROJECT_FILES.items():
                destination = project_dir / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if relative_path == "settings.py":
                    contents = contents.replace(
                        "__LENAPI_GENERATED_SECRET_KEY__", secrets.token_urlsafe(48)
                    )
                    contents = contents.replace(
                        "__LENAPI_GENERATED_API_KEY__", secrets.token_urlsafe(32)
                    )
                destination.write_text(contents, encoding="utf-8")
        except OSError as exc:
            import shutil
            shutil.rmtree(project_dir, ignore_errors=True)
            parser.error(f"could not create project: {exc}")
        print(f"Created lenApi project in {project_dir}")
        print(f"Next: cd {args.name} && python lenora.py migration && python lenora.py run")
        return

    if args.command == "repair-settings":
        project_dir = Path.cwd()
        settings_path = project_dir / "settings.py"
        if not (project_dir / "lenora.py").is_file():
            parser.error("run `python lenora.py repair-settings` from a lenApi project directory")
        if settings_path.exists():
            parser.error("settings.py already exists; it was not changed")

        contents = PROJECT_FILES["settings.py"]
        contents = contents.replace(
            "__LENAPI_GENERATED_SECRET_KEY__", secrets.token_urlsafe(48)
        ).replace(
            "__LENAPI_GENERATED_API_KEY__", secrets.token_urlsafe(32)
        )
        created = False
        try:
            with settings_path.open("x", encoding="utf-8") as settings_file:
                created = True
                settings_file.write(contents)
        except FileExistsError:
            parser.error("settings.py already exists; it was not changed")
        except OSError as exc:
            if created:
                settings_path.unlink(missing_ok=True)
            parser.error(f"could not recreate settings.py: {exc}")
        print(f"Recreated {settings_path} with default project settings and fresh keys.")
        print("Review the settings before starting the app; provider credentials remain blank.")
        return

    if args.command == "apikey":
        settings_path = Path.cwd() / "settings.py"
        models_path = Path.cwd() / "app" / "models.py"
        if not settings_path.is_file() or not models_path.is_file():
            parser.error("run `python lenora.py apikey` from a project with settings.py and app/models.py")

        settings_source = settings_path.read_text(encoding="utf-8")
        settings_lines = settings_source.splitlines()
        auth_setting_found = False
        for index, line in enumerate(settings_lines):
            key, separator, _value = line.partition("=")
            if separator and key.strip() == "AUTHENTICATION":
                settings_lines[index] = 'AUTHENTICATION = "api_key"'
                auth_setting_found = True
                break
        if not auth_setting_found:
            settings_lines.extend(['', 'AUTHENTICATION = "api_key"'])
        settings_path.write_text("\n".join(settings_lines) + "\n", encoding="utf-8")

        models_source = models_path.read_text(encoding="utf-8")
        has_api_key_model = any(
            line.lstrip().startswith("class APIKey(")
            for line in models_source.splitlines()
        )
        if not has_api_key_model:
            api_key_model = '''\n\n# Added by `python lenora.py apikey`.
from datetime import datetime
from lenapi import model


class APIKey(model.Model):
    user_id = model.ForeignKeyField("user.id", model=model.CASCADE, nullable=False)
    key_hash = model.StringField(max_length=64, unique=True, nullable=False)
    is_active = model.BooleanField(default=True, nullable=False)
    created_at = model.DateTimeField(default=datetime.utcnow, nullable=False)
    revoked_at = model.DateTimeField(nullable=True)
'''
            with models_path.open("a", encoding="utf-8") as models_file:
                models_file.write(api_key_model)
            print(f"Added APIKey model to {models_path}")
        else:
            print("APIKey model already exists; leaving app/models.py unchanged.")

        print('Enabled AUTHENTICATION = "api_key" in settings.py.')
        # Re-enter the standard migration path so existing revisions are
        # checked and only actual schema changes are generated and applied.
        sys.argv = [
            sys.argv[0], "migration", "--app", args.app,
            "--message", "activate API-key authentication",
        ]
        _main()
        return

    if args.command in {"generate_test", "run_test", "run_test_output", "test"}:
        module_name, separator, attribute_path = args.app.partition(":")
        if not separator or not module_name:
            parser.error("--app must use the form package.module:app")
        app = import_module(module_name)
        for attribute in attribute_path.split("."):
            app = getattr(app, attribute)
        if not hasattr(app, "routes") or not hasattr(app, "websocket_routes"):
            parser.error(f"{args.app} is not a lenApi application")

        http_endpoints = sorted(
            (method.lower(), route.path)
            for route in app.routes
            for method in route.methods
        )
        websocket_endpoints = sorted(route.path for route in app.websocket_routes)
        generated_test_temp = None
        if args.command in {"generate_test", "test"}:
            if args.command == "generate_test":
                test_dir = Path.cwd() / "tests"
                test_dir.mkdir(parents=True, exist_ok=True)
                destination = test_dir / "test_generated_endpoints.py"
            else:
                generated_test_temp = tempfile.TemporaryDirectory(prefix="lenapi-generated-tests-")
                destination = Path(generated_test_temp.name) / "test_generated_endpoints.py"
            websocket_test_source = (
                '''@pytest.mark.parametrize("path", WEBSOCKET_ENDPOINTS)
def test_websocket_endpoint_is_registered(path):
    app = load_app()
    assert any(route.path == path for route in app.websocket_routes)
'''
                if websocket_endpoints else ""
            )
            generated_source = f'''"""Generated endpoint registration and OpenAPI coverage."""

from importlib import import_module
import sys

import pytest

from lenapi.openapi import build_openapi

APP_IMPORT_STRING = {args.app!r}
HTTP_ENDPOINTS = {http_endpoints!r}
WEBSOCKET_ENDPOINTS = {websocket_endpoints!r}
PROJECT_ROOT = {str(Path.cwd().resolve())!r}
FRAMEWORK_ROOT = {str(Path(__file__).resolve().parents[1])!r}


def load_app():
    # Resolve the app from this project even when its parent directory has a
    # different main.py. Put the installed/source framework first so a
    # project-local compatibility module cannot shadow the lenapi package.
    for path in (PROJECT_ROOT, FRAMEWORK_ROOT):
        while path in sys.path:
            sys.path.remove(path)
    sys.path.insert(0, PROJECT_ROOT)
    sys.path.insert(0, FRAMEWORK_ROOT)
    module_name, _, attribute_path = APP_IMPORT_STRING.partition(":")
    app = import_module(module_name)
    for attribute in attribute_path.split(".") if attribute_path else []:
        app = getattr(app, attribute)

    return app


@pytest.mark.parametrize(("method", "path"), HTTP_ENDPOINTS)
def test_http_endpoint_is_registered_and_documented(method, path):
    app = load_app()
    assert any(route.path == path and method.upper() in route.methods for route in app.routes)
    openapi = build_openapi(app, title=app.title, version=app.version)
    assert method in openapi["paths"][path]

{websocket_test_source}
'''
            destination.write_text(generated_source, encoding="utf-8")
            print(f"Generated endpoint checks: {len(http_endpoints) + len(websocket_endpoints)}")
            if args.command == "generate_test":
                print(f"Generated {destination}")
            print(f"HTTP endpoints covered: {len(http_endpoints)}")
            for method, path in http_endpoints:
                print(f"  {method.upper():7} {path}")
            for path in websocket_endpoints:
                print(f"  WEBSOCKET {path}")
            if args.command == "generate_test":
                return

        print("Endpoints covered by generated route tests:")
        for method, path in http_endpoints:
            print(f"  {method.upper():7} {path}")
        for path in websocket_endpoints:
            print(f"  WEBSOCKET {path}")
        sys.stdout.flush()

        if args.command == "test":
            pytest_args = [*args.paths]
            if not pytest_args and (Path.cwd() / "tests").is_dir():
                pytest_args.append("tests")
            pytest_args.append(str(destination))
            project_dir = Path.cwd().resolve()
            pytest_args = [
                str((project_dir / path).resolve()) if not Path(path).is_absolute() else path
                for path in pytest_args
            ]
        elif args.paths:
            pytest_args = [*args.paths]
        else:
            pytest_args = []
            if (Path.cwd() / "test.py").is_file():
                pytest_args.append("test.py")
            if (Path.cwd() / "tests").is_dir():
                pytest_args.append("tests")
            if not pytest_args:
                pytest_args = ["tests"]
        command = [sys.executable, "-m", "pytest", "-q"]
        if args.command in {"run_test_output", "test"}:
            with tempfile.TemporaryDirectory(prefix="lenapi-pytest-") as temp_dir:
                junit_path = Path(temp_dir) / "results.xml"
                run_options = {}
                if args.command == "test":
                    project_dir = Path.cwd().resolve()
                    framework_root = str(Path(__file__).resolve().parents[1])
                    environment = os.environ.copy()
                    search_paths = [framework_root, str(project_dir)]
                    if environment.get("PYTHONPATH"):
                        search_paths.append(environment["PYTHONPATH"])
                    environment["PYTHONPATH"] = os.pathsep.join(search_paths)
                    run_options = {"cwd": project_dir.parent, "env": environment}
                result = subprocess.run(
                    [*command, "--junitxml", str(junit_path), *pytest_args], check=False,
                    **run_options,
                )
                if not junit_path.is_file():
                    logger.error("pytest did not create its JUnit report; see the pytest output above.")
                    if generated_test_temp is not None:
                        generated_test_temp.cleanup()
                    raise SystemExit(result.returncode or 1)
                suites = ET.parse(junit_path).getroot()
                cases = suites.findall(".//testcase")
                case_results = []
                for case in cases:
                    issue = next(
                        (case.find(tag) for tag in ("failure", "error", "skipped") if case.find(tag) is not None),
                        None,
                    )
                    outcome = "SKIP" if issue is not None and issue.tag == "skipped" else (
                        "FAIL" if issue is not None else "PASS"
                    )
                    details = "" if issue is None else (issue.get("message") or issue.text or "").strip()
                    case_results.append((case.get("classname", ""), case.get("name", ""), outcome, details))
                passed = sum(outcome == "PASS" for _class_name, _name, outcome, _details in case_results)
                failed = sum(outcome == "FAIL" for _class_name, _name, outcome, _details in case_results)
                skipped = sum(outcome == "SKIP" for _class_name, _name, outcome, _details in case_results)
                report_path = Path(args.output).expanduser()
                if not report_path.is_absolute():
                    report_path = Path.cwd() / report_path
                report_path.parent.mkdir(parents=True, exist_ok=True)
                with report_path.open("w", newline="", encoding="utf-8") as report_file:
                    writer = csv.writer(report_file)
                    writer.writerow(["check_type", "method", "endpoint", "result", "details"])
                    writer.writerow(["pytest summary", "", "", "PASS" if result.returncode == 0 else "FAIL",
                                     f"{passed} passed, {failed} failed, {skipped} skipped"])
                    endpoint_result = "PASS" if result.returncode == 0 else "REVIEW"
                    for method, path in http_endpoints:
                        writer.writerow([
                            "generated route/OpenAPI check", method.upper(), path, endpoint_result,
                            "See pytest case rows for the suite outcome",
                        ])
                    for path in websocket_endpoints:
                        writer.writerow([
                            "generated WebSocket route check", "WEBSOCKET", path, endpoint_result,
                            "See pytest case rows for the suite outcome",
                        ])
                    for class_name, name, outcome, details in case_results:
                        writer.writerow(["pytest case", "", f"{class_name}::{name}", outcome, details])
                print(f"CSV report written to {report_path}")
        else:
            result = subprocess.run([*command, *pytest_args], check=False)
        if generated_test_temp is not None:
            generated_test_temp.cleanup()
        raise SystemExit(result.returncode)

    if args.command == "run":
        try:
            import uvicorn
        except ImportError:
            parser.error("run requires Uvicorn; install LenApi with `python -m pip install lenapi`")
        print(f"lenApi server: http://{args.host}:{args.port}", flush=True)
        uvicorn.run(args.app, host=args.host, port=args.port, reload=args.reload)
        return

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        parser.error("migrations require Alembic; install lenApi dependencies with `pip install -e .`")

    ini_path = Path.cwd() / "alembic.ini"
    if not ini_path.is_file():
        parser.error(f"{ini_path} not found; run this command from your lenApi project directory")
    config = Config(str(ini_path))
    config.attributes["app_import_string"] = args.app
    # Register project ORM classes before Alembic reads Model.metadata. Do not
    # rely on main.py importing app.models as a side effect.
    try:
        import_module("app.models")
    except ModuleNotFoundError as exc:
        if exc.name not in {"app", "app.models"}:
            raise
    module_name, _, attribute_path = args.app.partition(":")
    app = import_module(module_name)
    for attribute in attribute_path.split(".") if attribute_path else []:
        app = getattr(app, attribute)

    if args.command == "admin":
        from getpass import getpass

        username = input("Admin username: ").strip()
        password = getpass("Password: ")
        confirm_password = getpass("Confirm password: ")
        email = input("Email: ").strip()
        if password != confirm_password:
            parser.error("passwords do not match")
        if app.docs_guard is None:
            parser.error("admin accounts require a configured SQL or MongoDB database")
        try:
            app.docs_guard.create_admin(username, email, password)
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print("Administrator account created successfully.")
        if app.mongodb is not None:
            import asyncio
            asyncio.run(app.mongodb.aclose())
        elif app.database is not None:
            app.database.close()
        return

    if app.mongodb is not None:
        if args.command == "background-worker":
            _run_registered_background_worker(module_name, app)
            return
        if args.command == "delete_migrations":
            answer = input("Delete all MongoDB collections and local migration revisions? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Cancelled; no collections or migration files were deleted.")
                import asyncio
                asyncio.run(app.mongodb.aclose())
                return
            import asyncio
            try:
                database = app.mongodb.database
                collection_names = database.list_collection_names()
                for collection_name in collection_names:
                    database.drop_collection(collection_name)
                revisions_dir = Path.cwd() / "migrations" / "versions"
                for path in revisions_dir.glob("*.py") if revisions_dir.exists() else []:
                    if path.name != "__init__.py":
                        path.unlink()
                logger.info("Deleted %d MongoDB collections and local migration revisions.", len(collection_names))
            finally:
                asyncio.run(app.mongodb.aclose())
            return
        if args.command == "showmigrations":
            print("MongoDB does not use Alembic revisions; LenApi initializes imported document collections.")
            return
        if args.command in {"repair-migrations", "clear_migrations"}:
            parser.error("this migration command applies only to SQL databases; MongoDB has no Alembic history")
        from .mongodb import MongoDocument

        def document_models(base):
            models = []
            for document_model in base.__subclasses__():
                models.append(document_model)
                models.extend(document_models(document_model))
            return models

        import asyncio
        created_collections = []
        try:
            existing_collections = set(app.mongodb.database.list_collection_names())
            collection_names_to_create = {
                document_model._collection_name()
                for document_model in document_models(MongoDocument)
            }
            collection_names_to_create.update({
                "admin_user",
                "lenapi_admin_sessions",
                "lenapi_security_store",
            })
            for collection_name in sorted(collection_names_to_create):
                if collection_name not in existing_collections:
                    app.mongodb.database.create_collection(collection_name)
                    existing_collections.add(collection_name)
                    created_collections.append(collection_name)
            app.mongodb.collection("admin_user").create_index("username", unique=True)
            app.mongodb.collection("admin_user").create_index("email", unique=True)
        finally:
            asyncio.run(app.mongodb.aclose())
        if created_collections:
            logger.info("Created MongoDB collections: %s", ", ".join(sorted(created_collections)))
        else:
            logger.info("MongoDB is up to date; no collections created.")
        return

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import MetaData, create_engine, inspect, text
    from lenapi import Model

    engine = app.database.engine
    revisions = Path.cwd() / "migrations" / "versions"
    if args.command == "delete_migrations":
        answer = input("Delete all tables and local migration revisions from this database? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled; no tables or migration files were deleted.")
            return
        with engine.connect() as connection:
            inspector = inspect(connection)
            table_names = set(inspector.get_table_names())
            view_names = set(inspector.get_view_names())
            dialect = connection.dialect.name
            quote = connection.dialect.identifier_preparer.quote
            if dialect == "mysql":
                connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
            elif dialect == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                table_names = {name for name in table_names if not name.startswith("sqlite_")}
            try:
                for name in sorted(view_names):
                    connection.exec_driver_sql(f"DROP VIEW {quote(name)}")
                for name in sorted(table_names):
                    cascade = " CASCADE" if dialect == "postgresql" else ""
                    connection.exec_driver_sql(f"DROP TABLE {quote(name)}{cascade}")
                connection.commit()
            finally:
                if dialect == "mysql":
                    connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        engine.dispose()
        if dialect == "sqlite":
            database_path = engine.url.database
            if database_path and database_path != ":memory:":
                db_path = Path(database_path)
                for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
                    path.unlink(missing_ok=True)
        for path in revisions.glob("*.py") if revisions.exists() else []:
            if path.name != "__init__.py":
                path.unlink()
        logger.info(
            "Deleted %d database tables, %d views, and local migration revisions.",
            len(table_names), len(view_names),
        )
        return

    if args.command == "background-worker":
        config.attributes["lenapi_include_background_jobs"] = True
    else:
        with engine.connect() as connection:
            background_table_exists = "lenapi_background_jobs" in inspect(connection).get_table_names()
        background_migration_exists = any(
            "lenapi_background_jobs" in path.read_text(encoding="utf-8")
            for path in revisions.glob("*.py")
            if path.name != "__init__.py"
        ) if revisions.exists() else False
        config.attributes["lenapi_include_background_jobs"] = (
            background_table_exists or background_migration_exists
        )

    def include_migration_object(obj, name, type_, reflected, compare_to):
        if type_ == "table" and name == "lenapi_background_jobs":
            return config.attributes.get("lenapi_include_background_jobs", False)
        return True

    if args.command == "showmigrations":
        import ast

        local_revisions = []
        for path in sorted(revisions.glob("*.py")):
            if path.name == "__init__.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                values = {}
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, ast.AnnAssign):
                        targets = [node.target]
                    else:
                        continue
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(node.value)
                revision_id = values.get("revision")
                if revision_id:
                    doc = ast.get_docstring(tree) or path.stem
                    parents = values.get("down_revision")
                    local_revisions.append((str(revision_id), parents, doc, path.name))
            except (SyntaxError, ValueError) as exc:
                logger.warning("Could not read migration metadata from %s: %s", path.name, exc)
        with engine.connect() as connection:
            current_heads = set(MigrationContext.configure(connection).get_current_heads())
        known_ids = {revision_id for revision_id, _, _, _ in local_revisions}
        parent_map = {revision_id: parents for revision_id, parents, _, _ in local_revisions}
        applied = set()
        for current in current_heads & known_ids:
            pending = [current]
            while pending:
                revision_id = pending.pop()
                if revision_id in applied or revision_id not in parent_map:
                    continue
                applied.add(revision_id)
                parents = parent_map[revision_id]
                if parents is None:
                    continue
                pending.extend(parents if isinstance(parents, (tuple, list, set)) else [parents])
        if not local_revisions:
            print("No local migration revisions.")
        for revision_id, parents, doc, filename in local_revisions:
            state = "[X]" if revision_id in applied else "[ ]"
            parent_text = ", ".join(parents) if isinstance(parents, (tuple, list, set)) else (parents or "base")
            print(f"{state} {revision_id} {doc} (down: {parent_text}; {filename})")
        missing_parents = set()
        for revision_id, parents, _, _ in local_revisions:
            if parents is None:
                continue
            parent_ids = parents if isinstance(parents, (tuple, list, set)) else [parents]
            missing_parents.update(parent for parent in parent_ids if parent not in known_ids)
        for missing in sorted(missing_parents):
            print(f"[!] {missing} (referenced by a local migration but its file is missing)")
        for unknown in sorted(current_heads - known_ids):
            print(f"[!] {unknown} (database references a revision missing from migrations/versions)")
        if not current_heads:
            print("Database migration state: no revision recorded.")
        elif current_heads <= known_ids:
            print("Database migration state: " + ", ".join(sorted(current_heads)))
        return

    if args.command == "repair-migrations":
        script = ScriptDirectory.from_config(config)
        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            differences = compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={"compare_type": True, "include_object": include_migration_object},
                ),
                Model.metadata,
            )
        if differences:
            parser.error(
                "database schema does not match current models; refusing to change migration history. "
                "Restore the missing revision file or reconcile the schema first. "
                "If the database is empty, run python lenora.py migration instead."
            )
        local_revisions = [
            path for path in revisions.glob("*.py") if path.name != "__init__.py"
        ]
        if not local_revisions:
            if not table_names:
                parser.error("database is empty; run python lenora.py migration to create the initial revision")
            from alembic.autogenerate import render_python_code
            from alembic.operations.ops import CreateIndexOp, CreateTableOp, UpgradeOps
            from alembic.util import rev_id

            revision_id = rev_id()
            operations = []
            tables = list(Model.metadata.sorted_tables)
            for table in tables:
                operations.append(CreateTableOp.from_table(table))
            for table in tables:
                operations.extend(
                    CreateIndexOp.from_index(index)
                    for index in sorted(table.indexes, key=lambda index: index.name or "")
                )
            upgrade_code = render_python_code(UpgradeOps(ops=operations))
            downgrade_code = "\n".join(
                f"    op.drop_table({table.name!r})" for table in reversed(tables)
            ) or "    pass"
            revisions.mkdir(parents=True, exist_ok=True)
            baseline = revisions / f"{revision_id}_baseline.py"
            baseline.write_text(
                '"""Baseline schema captured from the current model metadata.\n\n'
                f"Revision ID: {revision_id}\nRevises: None\n\n\n"
                '"""\nfrom alembic import op\nimport sqlalchemy as sa\n\n'
                f"revision = {revision_id!r}\ndown_revision = None\n"
                "branch_labels = None\ndepends_on = None\n\n\n"
                "def upgrade():\n"
                f"{upgrade_code}\n\n\n"
                "def downgrade():\n"
                f"{downgrade_code}\n",
                encoding="utf-8",
            )
            logger.info("Created baseline migration %s from current models.", baseline.name)
            head = revision_id
        else:
            head = script.get_current_head()
            if head is None:
                parser.error("no migration head found; inspect showmigrations and restore missing revision files")
        command.stamp(config, "head")
        logger.info("Migration history repaired to %s; schema and data were preserved.", head)
        return

    if args.command == "clear_migrations":
        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
        application_tables = table_names - {"alembic_version"}
        if application_tables:
            parser.error(
                "refusing to clear migrations because the database contains application tables; "
                "use `python lenora.py repair-migrations` for a missing revision when its schema matches models"
            )
        if "alembic_version" in table_names:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE alembic_version"))
        if revisions.exists():
            for path in revisions.glob("*.py"):
                if path.name != "__init__.py":
                    path.unlink()
        logger.info("Cleared migration files and database history; no application tables existed.")
        return

    schema_generation = args.command in {"migration", "background-worker"}
    if schema_generation:
        existing_revisions = [
            path for path in revisions.glob("*.py") if path.name != "__init__.py"
        ]
        revisions_before_autogenerate = {path.resolve() for path in existing_revisions}
        config.attributes["lenapi_initial_revision"] = not existing_revisions

        # On a fresh non-SQLite database, apply the checked-in migration chain
        # before asking Alembic to autogenerate the next revision. Otherwise
        # Alembic rejects revision generation because the database is behind
        # the current repository head.
        if existing_revisions and engine.dialect.name != "sqlite":
            with engine.connect() as connection:
                table_names = set(inspect(connection).get_table_names())
            if not table_names:
                script = ScriptDirectory.from_config(config)
                command.upgrade(config, script.get_current_head())

        # Adopt databases created from this project's prior migration history.
        # Verify against the already-recorded migration schema before stamping;
        # never mark an unrelated or mismatched schema as migrated.
        if existing_revisions and engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                table_names = set(inspect(connection).get_table_names())
                has_version = "alembic_version" in table_names
                if has_version:
                    has_version = connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version LIMIT 1"
                    ).first() is not None
            user_tables = table_names - {"alembic_version"}
            if not has_version:
                script = ScriptDirectory.from_config(config)
                existing_head = script.get_current_head()
                if not user_tables:
                    command.upgrade(config, existing_head)
                else:
                    temporary_engine = create_engine("sqlite://")
                    try:
                        with temporary_engine.connect() as temporary_connection:
                            config.attributes["connection"] = temporary_connection
                            command.upgrade(config, existing_head)
                            config.attributes.pop("connection", None)
                            expected_schema = MetaData()
                            expected_schema.reflect(bind=temporary_connection)
                        version_table = expected_schema.tables.get("alembic_version")
                        if version_table is not None:
                            expected_schema.remove(version_table)
                        with engine.connect() as connection:
                            differences = compare_metadata(
                                MigrationContext.configure(
                                    connection,
                                    opts={"compare_type": True, "include_object": include_migration_object},
                                ),
                                expected_schema,
                            )
                        if differences:
                            parser.error(
                                "existing database does not match its recorded migration history; "
                                "review the schema before migrating"
                            )
                        command.stamp(config, existing_head)
                        logger.info("Existing database matches its migration history; preserving its data.")
                    finally:
                        config.attributes.pop("connection", None)
                        temporary_engine.dispose()

        def skip_empty_revision(migration_context, revision, directives):
            if directives and directives[0].upgrade_ops.is_empty():
                directives.clear()

        command.revision(
            config,
            message=args.message,
            autogenerate=True,
            process_revision_directives=skip_empty_revision,
        )
        revisions_after_autogenerate = {
            path.resolve()
            for path in revisions.glob("*.py")
            if path.name != "__init__.py"
        }
        if revisions_after_autogenerate == revisions_before_autogenerate:
            logger.info("No new migration detected; database was not migrated.")
            if args.command == "background-worker":
                _run_registered_background_worker(module_name, app)
            return

    # `migration` generates then applies; `migrate` only applies revisions already created.
    if not any(path.name != "__init__.py" for path in revisions.glob("*.py")):
        if args.command == "migrate":
            parser.error("no migration revisions found; run `python lenora.py migration` first")
        logger.info("No schema changes detected.")
        return

    # When adopting a pre-migration SQLite file, recognize the earlier
    # pluralized table name and rename it without dropping its existing rows.
    if schema_generation:
        with engine.begin() as connection:
            inspector = inspect(connection)
            existing = set(inspector.get_table_names())
            if "alembic_version" not in existing and connection.dialect.name == "sqlite":
                quote = connection.dialect.identifier_preparer.quote
                for table_name, table in Model.metadata.tables.items():
                    old_name = f"{table_name}s"
                    if table_name not in existing and old_name in existing:
                        old_columns = {column["name"] for column in inspector.get_columns(old_name)}
                        if old_columns == set(table.columns.keys()):
                            connection.execute(text(f"ALTER TABLE {quote(old_name)} RENAME TO {quote(table_name)}"))
                            existing.remove(old_name)
                            existing.add(table_name)

    mark_existing_schema = False
    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        existing_tables = table_names - {"alembic_version"}
        if existing_tables and "alembic_version" not in table_names:
            differences = compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={"compare_type": True, "include_object": include_migration_object},
                ),
                Model.metadata,
            )
            if differences:
                parser.error("existing database schema differs from models; review it before applying the initial migration")
            mark_existing_schema = True
    if mark_existing_schema:
        command.stamp(config, "head")
        logger.info("Existing database matches the models; migration history initialized without changing its data.")

    script = ScriptDirectory.from_config(config)
    with engine.connect() as connection:
        current_heads = set(MigrationContext.configure(connection).get_current_heads())
    expected_heads = set(script.get_heads())
    if current_heads == expected_heads:
        logger.info("Database is already up to date; no migrations applied.")
        if args.command == "background-worker":
            _run_registered_background_worker(module_name, app)
        return
    command.upgrade(config, "head")
    if args.command == "background-worker":
        _run_registered_background_worker(module_name, app)


def main() -> None:
    """Run the CLI and print an explicit result for migration commands."""
    command_name = sys.argv[1] if len(sys.argv) > 1 else None
    is_migration = command_name in {"migration", "migrate", "background-worker", "showmigrations", "repair-migrations", "clear_migrations", "delete_migrations", "apikey"}
    if is_migration:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        _main()
    except SystemExit as exc:
        if is_migration and exc.code not in (None, 0):
            logger.error("Migration failed: exit status %s", exc.code)
        raise
    except Exception as exc:
        if is_migration:
            logger.error("Migration failed: %s", str(exc) or "unknown error")
        raise
    else:
        if is_migration:
            logger.info("Migration successful.")
