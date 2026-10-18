"""Command line interface for lenApi projects."""

import argparse
import csv
from importlib import import_module
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from .logging import logger


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8009
DEFAULT_APP = "main:app"


PROJECT_FILES = {
    "__init__.py": "",
    ".gitignore": "__pycache__/\n.pytest_cache/\n*.py[cod]\n.env\nlenapi.db\nmedia/\n.venv/\nbuild/\ndist/\n*.egg-info/\n",
    "settings.py": '''"""Project settings for lenApp."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
API_TITLE = "lenApp"
API_VERSION = "0.1.0"
DOCS_URL = "/docs/"
OPENAPI_URL = "/openapi.json"
# Limit incoming request bodies to 10 MiB; set to None to disable.
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024
# Maximum time allowed to receive an incoming body; set to None to disable.
REQUEST_BODY_TIMEOUT_SECONDS = 30
# Shared storage for IP flags, sessions, opaque tokens, and HMAC replay checks.
# Choose "database" (requires migration), "redis", or "memory" (development only).
SECURITY_STORAGE = "database"
REDIS_URL = "redis://127.0.0.1:6379/0"
CACHE_BACKEND = "memory"  # "memory", "database", or "redis".
# runserver uses these values; CLI flags override them.
HOST = os.getenv("LENAPI_HOST", "127.0.0.1")
PORT = int(os.getenv("LENAPI_PORT", "8009"))
DATABASE_URL = os.getenv("LENAPI_DATABASE_URL", f"sqlite:///{BASE_DIR / 'lenapi.db'}")
ASYNC_DATABASE_URL = os.getenv("LENAPI_ASYNC_DATABASE_URL")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE")
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"
# Choose one primary method: "jwt", "api_key", "basic_login", "opaque_bearer", or "session".
# Session can also be added to selected routes; add HMAC/OIDC with Depends(...).
AUTHENTICATION = "jwt"
BASIC_AUTH_FIELD = "username"  # Basic login field on User: username, email, phone, etc.
API_KEY = "__LENAPI_GENERATED_API_KEY__"
# Keep this generated key private. Rotate it to invalidate existing tokens.
SECRET_KEY = "__LENAPI_GENERATED_SECRET_KEY__"
TOKEN = {
    "SECRET_KEY": SECRET_KEY,
    "LIFETIME_SECONDS": 3600,
    "REFRESH_LIFETIME_SECONDS": 2592000,
    "ISSUER": API_TITLE,
}
''',
    "main.py": '''"""Application entry point."""

from lenapi import Depends, LenApi, check_password, create_authenticator, hash_api_key, make_api_key, make_password
from app import models  # Import models so migrations can discover them.
import settings
from pydantic import BaseModel, ConfigDict, Field, model_validator

app = LenApi()

if settings.AUTHENTICATION == "basic_login":
    identifier_field = getattr(settings, "BASIC_AUTH_FIELD", "username")
    if identifier_field not in models.User.__table__.columns:
        raise ValueError(f"BASIC_AUTH_FIELD '{identifier_field}' is not a field on User")


def authenticate_user(username, password):
    user = models.User.get(username=username).get()
    if user is not None and check_password(password, user.password):
        return str(user.id)
    return None


def authenticate_api_key(api_key):
    if not api_key:
        return None
    user = models.User.get(api_key=hash_api_key(api_key)).get()
    return {"user_id": str(user.id), "auth_method": "api_key"} if user is not None else None


authenticator = create_authenticator(
    settings, authenticate=authenticate_user, authenticate_api_key=authenticate_api_key,
    security_store=app.security_store,
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
''',
    "app/__init__.py": '''"""Application package."""
''',
    "app/models.py": '''"""Define database models here."""

from lenapi import model


class User(model.Model):
    username = model.StringField(max_length=120, unique=True)
    email = model.StringField(max_length=254, unique=True, nullable=True)
    first_name = model.StringField(max_length=150, default="")
    last_name = model.StringField(max_length=150, default="")
    password = model.StringField(max_length=255)
    api_key = model.StringField(max_length=64, unique=True, nullable=True)
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

Project settings are in `settings.py`; models belong in `app/models.py` and routes in `main.py`.

```bash
python lenora.py migration
python lenora.py runserver
```

The development server uses port 8009. Open `/docs/` for Swagger UI. Run `python lenora.py run_test` to run `test.py` and any generated endpoint tests.
''',
    "test.py": '''"""Starter smoke tests for the application."""

from main import app


def test_root_route_is_registered():
    assert any(route.path == "/" and "GET" in route.methods for route in app.routes)
''',
    "pytest.ini": '''[pytest]
python_files = test.py test_*.py *_test.py
''',
}


def _main() -> None:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="lenApi project tools",
        epilog=(
            "Examples:\n"
            "  python lenora.py startproject myproject\n"
            "  python lenora.py runserver --port 8010\n"
            "  python lenora.py migration --message add_users\n\n"
            "For flags and arguments for a command, run: "
            "python lenora.py <command> --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    runserver = commands.add_parser("runserver", help="run the ASGI development server")
    runserver.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    runserver.add_argument("--host", help="host to bind (default: project settings or 127.0.0.1)")
    runserver.add_argument("--port", type=int, help="port to bind (default: project settings or 8009)")
    reload_options = runserver.add_mutually_exclusive_group()
    reload_options.add_argument("--reload", dest="reload", action="store_true", help="reload when project files change (default)")
    reload_options.add_argument("--no-reload", dest="reload", action="store_false", help="disable automatic reload")
    runserver.set_defaults(reload=True)

    startproject = commands.add_parser("startproject", help="create a new lenApi project")
    startproject.add_argument("name", nargs="?", default="lenApp", help="project folder name (default: lenApp)")

    migration = commands.add_parser("migration", help="generate and apply a migration from model changes")
    migration.add_argument("--message", "-m", default="schema update", help="migration description")
    migration.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")
    migrate = commands.add_parser("migrate", help="apply pending migrations to the database")
    migrate.add_argument("--app", default=DEFAULT_APP, help=f"ASGI app import string (default: {DEFAULT_APP})")

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

    args = parser.parse_args()

    if args.command == "runserver":
        project_settings = None
        if (Path.cwd() / "settings.py").is_file():
            project_settings = import_module("settings")
        args.host = args.host or os.getenv("LENAPI_HOST") or getattr(project_settings, "HOST", DEFAULT_HOST)
        args.port = args.port or int(os.getenv("LENAPI_PORT", getattr(project_settings, "PORT", DEFAULT_PORT)))

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
        print(f"Next: cd {args.name} && python lenora.py migration && python lenora.py runserver")
        return

    if args.command in {"generate_test", "run_test", "run_test_output"}:
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
        if args.command == "generate_test":
            test_dir = Path.cwd() / "tests"
            test_dir.mkdir(parents=True, exist_ok=True)
            destination = test_dir / "test_generated_endpoints.py"
            websocket_test_source = (
                '''@pytest.mark.parametrize("path", WEBSOCKET_ENDPOINTS)
def test_websocket_endpoint_is_registered(path):
    app = load_app()
    assert any(route.path == path for route in app.websocket_routes)
'''
                if websocket_endpoints else ""
            )
            generated_source = f'''"""Generated endpoint registration and OpenAPI coverage. Regenerate with python lenora.py generate_test."""

from importlib import import_module

import pytest

from lenapi.openapi import build_openapi

APP_IMPORT_STRING = {args.app!r}
HTTP_ENDPOINTS = {http_endpoints!r}
WEBSOCKET_ENDPOINTS = {websocket_endpoints!r}


def load_app():
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
            print(f"Generated {destination}")
            print(f"HTTP endpoints covered: {len(http_endpoints)}")
            for method, path in http_endpoints:
                print(f"  {method.upper():7} {path}")
            for path in websocket_endpoints:
                print(f"  WEBSOCKET {path}")
            return

        print("Endpoints covered by generated route tests:")
        for method, path in http_endpoints:
            print(f"  {method.upper():7} {path}")
        for path in websocket_endpoints:
            print(f"  WEBSOCKET {path}")
        sys.stdout.flush()

        if args.paths:
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
        if args.command == "run_test_output":
            with tempfile.TemporaryDirectory(prefix="lenapi-pytest-") as temp_dir:
                junit_path = Path(temp_dir) / "results.xml"
                result = subprocess.run(
                    [*command, "--junitxml", str(junit_path), *pytest_args], check=False
                )
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
        raise SystemExit(result.returncode)

    if args.command == "runserver":
        try:
            import uvicorn
        except ImportError:
            parser.error("runserver requires Uvicorn; install lenApi with `pip install '.[server]'`")
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
    module_name, _, attribute_path = args.app.partition(":")
    app = import_module(module_name)
    for attribute in attribute_path.split(".") if attribute_path else []:
        app = getattr(app, attribute)
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import MetaData, create_engine, inspect
    from lenapi import Model

    engine = app.database.engine
    revisions = Path.cwd() / "migrations" / "versions"
    if args.command == "migration":
        existing_revisions = [
            path for path in revisions.glob("*.py") if path.name != "__init__.py"
        ]
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
                                    connection, opts={"compare_type": True}
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

    # `migration` generates then applies; `migrate` only applies revisions already created.
    from sqlalchemy import inspect, text

    if not any(path.name != "__init__.py" for path in revisions.glob("*.py")):
        if args.command == "migrate":
            parser.error("no migration revisions found; run `python lenora.py migration` first")
        logger.info("No schema changes detected.")
        return

    # When adopting a pre-migration SQLite file, recognize the earlier
    # pluralized table name and rename it without dropping its existing rows.
    if args.command == "migration":
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
            differences = compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Model.metadata)
            if differences:
                parser.error("existing database schema differs from models; review it before applying the initial migration")
            mark_existing_schema = True
    if mark_existing_schema:
        command.stamp(config, "head")
        logger.info("Existing database matches the models; migration history initialized without changing its data.")
    else:
        command.upgrade(config, "head")


def main() -> None:
    """Run the CLI and print an explicit result for migration commands."""
    command_name = sys.argv[1] if len(sys.argv) > 1 else None
    is_migration = command_name in {"migration", "migrate"}
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
