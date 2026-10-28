"""Project identity, namespaced database objects, and optional ASGI mounting."""

import os
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from lenapi import Application


ROOT = Path(__file__).resolve().parents[1]


def test_namespaced_models_and_mongodb_collections_are_project_scoped():
    source = r'''
from lenapi import model
from lenapi.mongodb import MongoDocument

class NamespaceParent(model.Model):
    value = model.StringField(max_length=20)

class NamespaceChild(model.Model):
    parent_id = model.ForeignKeyField("namespaceparent.id")
    links = model.ManyToManyField(NamespaceParent)

class NamespaceNote(MongoDocument):
    collection_name = "note"
    body: str

assert NamespaceParent.__table__.name == "workspace_alpha_namespaceparent"
assert NamespaceChild.__table__.name == "workspace_alpha_namespacechild"
assert NamespaceChild.__table__.c.parent_id.foreign_keys.pop().target_fullname == "workspace_alpha_namespaceparent.id"
assert NamespaceChild.links.property.secondary.name == "workspace_alpha_namespacechild_links"
assert NamespaceNote._collection_name() == "workspace_alpha_note"
'''
    env = os.environ.copy()
    env["LENAPI_PROJECT_NAMESPACE"] = "workspace_alpha"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_startproject_rejects_reserved_framework_name(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", "from lenapi.cli import main; main()", "startproject", "lenora"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "reserved for the LenAPI framework" in result.stderr
    assert not (tmp_path / "lenora").exists()


def test_generated_project_uses_namespaced_tables_and_requires_orphan_confirmation(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    created = subprocess.run(
        [sys.executable, "-c", "from lenapi.cli import main; main()", "startproject", "demo"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    project_dir = tmp_path / "demo"
    assert (project_dir / "settings.py").is_file()
    assert "PROJECT_NAME = \"demo\"" in (project_dir / "settings.py").read_text()

    migrated = subprocess.run(
        [sys.executable, "lenora.py", "migration"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr

    import sqlite3

    database_path = project_dir / "lenapi.db"
    legacy_revision = project_dir / "migrations" / "versions" / "legacy_revision.py"
    legacy_revision.write_text("from alembic import op\n\ndef upgrade():\n    op.create_table('legacy_table')\n")
    cleanup_declined = subprocess.run(
        [sys.executable, "lenora.py", "delete_migrations"],
        cwd=project_dir,
        env=env,
        input="n\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert cleanup_declined.returncode == 0, cleanup_declined.stdout + cleanup_declined.stderr
    assert "Cancelled" in cleanup_declined.stdout
    assert legacy_revision.is_file()
    legacy_revision.unlink()

    namespace = f"{tmp_path.name.lower()}_demo"
    if len(namespace) > 30:
        namespace = f"{namespace[:21].rstrip('_')}_{hashlib.sha256(namespace.encode()).hexdigest()[:8]}"
    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert f"{namespace}_user" in tables
        assert f"{namespace}_admin_user" in tables
        assert f"{namespace}_alembic_version" in tables
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('other_project_revision')")

    isolated_migrate = subprocess.run(
        [sys.executable, "lenora.py", "migrate"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert isolated_migrate.returncode == 0, isolated_migrate.stdout + isolated_migrate.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "other_project_revision"
        connection.execute(f'CREATE TABLE "{namespace}_orphan" (id INTEGER PRIMARY KEY)')

    declined = subprocess.run(
        [sys.executable, "lenora.py", "migration"],
        cwd=project_dir,
        env=env,
        input="n\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert declined.returncode != 0
    assert f"{namespace}_orphan" in declined.stdout
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (f"{namespace}_orphan",),
        ).fetchone()

    confirmed = subprocess.run(
        [sys.executable, "lenora.py", "migration"],
        cwd=project_dir,
        env=env,
        input="y\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (f"{namespace}_orphan",),
        ).fetchone() is None


def test_project_settings_identity_matches_project_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("LENAPI_PROJECT_NAME", raising=False)
    monkeypatch.delenv("LENAPI_PROJECT_NAMESPACE", raising=False)
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    from lenapi.project import configure_project_from_settings

    settings = type("Settings", (), {
        "PROJECT_NAME": "demo",
        "PROJECT_NAMESPACE": "workspace_demo",
    })
    assert configure_project_from_settings(settings, project_dir) == "workspace_demo"
    with pytest.raises(RuntimeError, match="Project identity mismatch"):
        configure_project_from_settings(
            type("Settings", (), {
                "PROJECT_NAME": "other",
                "PROJECT_NAMESPACE": "workspace_demo",
            }),
            project_dir,
        )


def test_mount_routes_docs_and_openapi_under_custom_project_prefix():
    import asyncio
    root = Application(database=False)
    child = Application(database=False)
    child.project_name = "games"

    @child.get("/health")
    def health():
        return {"project": "games"}

    root.mount("/gamer", child)
    import httpx

    async def fetch():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=root), base_url="http://test"
        ) as client:
            response = await client.get("/gamer/health")
            docs = await client.get("/gamer/docs/")
            schema = await client.get("/gamer/openapi.json")
            return response, docs, schema

    response, docs, schema = asyncio.run(fetch())

    assert response.status_code == 200
    assert response.json() == {"project": "games"}
    assert docs.status_code == 200
    assert "/gamer/openapi.json" in docs.text
    assert schema.status_code == 200
    assert schema.json()["servers"] == [{"url": "/gamer"}]


def test_mount_allows_prefix_to_differ_from_project_name():
    root = Application(database=False)
    child = Application(database=False)
    child.project_name = "games"
    root.mount("/gamer", child)
    assert root._mounted_apps["/gamer"] is child
