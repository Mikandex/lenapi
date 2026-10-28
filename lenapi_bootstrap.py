"""Project bootstrap helpers used by generated LenAPI applications.

This module is deliberately outside the ``lenapi`` package so it can set the
project database namespace before importing LenAPI's model classes.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType


def _load_settings(project_dir: Path, package: str | None) -> ModuleType:
    if package:
        return importlib.import_module(f"{package}.settings")

    settings_path = project_dir / "settings.py"
    module_name = f"_lenapi_settings_{project_dir.name}"
    spec = importlib.util.spec_from_file_location(module_name, settings_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load project settings from {settings_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def configure_project(source_file: str, package: str | None = None):
    """Load settings and configure the project namespace before importing LenAPI."""
    project_dir = Path(source_file).resolve().parent
    settings = _load_settings(project_dir, package)
    project_name = getattr(settings, "PROJECT_NAME", None)
    if not isinstance(project_name, str) or not project_name.isidentifier():
        raise RuntimeError("settings.PROJECT_NAME must be a valid Python identifier")
    if project_name.lower() == "lenora":
        raise RuntimeError("'lenora' is reserved for the LenAPI framework")
    if project_name != project_dir.name:
        raise RuntimeError(
            f"Project identity mismatch: settings.PROJECT_NAME is {project_name!r}, "
            f"but the project directory is {project_dir.name!r}. Set PROJECT_NAME to "
            "the directory name or restore the directory name."
        )
    namespace = getattr(settings, "PROJECT_NAMESPACE", None)
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,29}", namespace.lower()):
        raise RuntimeError("settings.PROJECT_NAMESPACE must be a valid database namespace")
    os.environ["LENAPI_PROJECT_NAME"] = project_name
    os.environ["LENAPI_PROJECT_NAMESPACE"] = namespace.lower()
    return settings


def create_app(source_file: str, package: str | None = None):
    """Create a configured app and import the project's models for migrations."""
    settings = configure_project(source_file, package)
    model_module = f"{package}.app.models" if package else "app.models"
    importlib.import_module(model_module)

    from lenapi import LENAPI

    app = LENAPI(settings=settings)
    app.project_dir = Path(source_file).resolve().parent
    return app


def load_project_app(project_dir: Path):
    """Import a sibling generated project's ``main:app`` safely."""
    project_dir = Path(project_dir).resolve()
    parent = str(project_dir.parent)
    previous_name = os.environ.get("LENAPI_PROJECT_NAME")
    previous_namespace = os.environ.get("LENAPI_PROJECT_NAMESPACE")
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        module = importlib.import_module(f"{project_dir.name}.main")
        app = getattr(module, "app", None)
        if app is None:
            raise ValueError(f"Project {project_dir.name!r} does not expose main.app")
        return app
    finally:
        if previous_name is None:
            os.environ.pop("LENAPI_PROJECT_NAME", None)
        else:
            os.environ["LENAPI_PROJECT_NAME"] = previous_name
        if previous_namespace is None:
            os.environ.pop("LENAPI_PROJECT_NAMESPACE", None)
        else:
            os.environ["LENAPI_PROJECT_NAMESPACE"] = previous_namespace
        if inserted:
            try:
                sys.path.remove(parent)
            except ValueError:
                pass
