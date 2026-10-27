"""Project-scoped database object naming helpers.

Generated project launchers set ``LENAPI_PROJECT_NAMESPACE`` before importing
LenAPI, so each project's SQL tables, Mongo collections, and migration marker
can coexist in a shared database.
"""

from __future__ import annotations

import os
import re
import hashlib


def project_namespace() -> str:
    """Return the validated namespace configured by a project launcher."""
    value = os.environ.get("LENAPI_PROJECT_NAMESPACE", "").strip().lower()
    if not value:
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,29}", value):
        raise ValueError(
            "PROJECT_NAMESPACE must start with a letter, contain only letters, "
            "numbers, and underscores, and be at most 30 characters"
        )
    return value


def project_object_name(name: str, *, namespace: str | None = None) -> str:
    """Prefix a database object name when running inside a generated project."""
    namespace = project_namespace() if namespace is None else namespace.strip().lower()
    if not namespace:
        return name
    prefix = f"{namespace}_"
    result = name if name.lower().startswith(prefix) else f"{prefix}{name}"
    if len(result) > 63:
        digest = hashlib.sha256(result.encode("utf-8")).hexdigest()[:8]
        result = f"{result[:54].rstrip('_')}_{digest}"
    return result


def configure_project_from_settings(settings, project_dir) -> str:
    """Validate a generated project's identity and export its DB namespace."""
    from pathlib import Path

    root = Path(project_dir).resolve()
    project_name = getattr(settings, "PROJECT_NAME", None)
    if not isinstance(project_name, str) or not project_name.isidentifier():
        raise RuntimeError("settings.PROJECT_NAME must be a valid Python identifier")
    if project_name.lower() == "lenora":
        raise RuntimeError("'lenora' is reserved for the LenAPI framework")
    if project_name != root.name:
        raise RuntimeError(
            f"Project identity mismatch: settings.PROJECT_NAME is {project_name!r}, "
            f"but the project directory is {root.name!r}. Set PROJECT_NAME to the "
            "directory name or restore the directory name."
        )
    namespace = getattr(settings, "PROJECT_NAMESPACE", None)
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", namespace.lower()):
        raise RuntimeError("settings.PROJECT_NAMESPACE must be a valid database namespace")
    os.environ["LENAPI_PROJECT_NAME"] = project_name
    os.environ["LENAPI_PROJECT_NAMESPACE"] = namespace.lower()
    return namespace.lower()
