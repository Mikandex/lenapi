import ast
import sys

import pytest

from lenapi import cli


def test_repair_settings_creates_missing_settings_with_fresh_keys(tmp_path, monkeypatch, capsys):
    (tmp_path / "lenora.py").write_text("# project launcher\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lenora.py", "repair-settings"])

    cli._main()

    settings_path = tmp_path / "settings.py"
    source = settings_path.read_text(encoding="utf-8")
    ast.parse(source)
    assert "SOCIAL_AUTH_ALLOWED_ORIGINS = []" in source
    assert "__LENAPI_GENERATED_SECRET_KEY__" not in source
    assert "__LENAPI_GENERATED_API_KEY__" not in source
    assert "Recreated" in capsys.readouterr().out


def test_repair_settings_never_overwrites_existing_file(tmp_path, monkeypatch):
    (tmp_path / "lenora.py").write_text("# project launcher\n", encoding="utf-8")
    settings_path = tmp_path / "settings.py"
    settings_path.write_text("CUSTOM_SETTING = True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lenora.py", "repair-settings"])

    with pytest.raises(SystemExit) as exc_info:
        cli._main()

    assert exc_info.value.code == 2
    assert settings_path.read_text(encoding="utf-8") == "CUSTOM_SETTING = True\n"


def test_repair_settings_requires_project_launcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lenora.py", "repair-settings"])

    with pytest.raises(SystemExit) as exc_info:
        cli._main()

    assert exc_info.value.code == 2
    assert not (tmp_path / "settings.py").exists()
