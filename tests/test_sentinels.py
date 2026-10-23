"""Tests for omitted-value sentinel behavior."""

import copy
import importlib
import pickle
import sys
from types import SimpleNamespace

from lenapi import Database, LENAPI, UNSET


def test_unset_is_a_stable_singleton():
    assert repr(UNSET) == "UNSET"
    assert copy.copy(UNSET) is UNSET
    assert copy.deepcopy(UNSET) is UNSET
    assert pickle.loads(pickle.dumps(UNSET)) is UNSET


def test_lenapi_distinguishes_omitted_overrides_from_explicit_none(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        DOCS_URL="/private-docs/",
        OPENAPI_URL="/schema.json",
        MAX_REQUEST_BODY_SIZE=4096,
        REQUEST_BODY_TIMEOUT_SECONDS=3,
        MEDIA_ROOT=tmp_path,
        MEDIA_URL="/assets/",
        DOCS_COOKIE_SECURE=True,
    )
    monkeypatch.setitem(sys.modules, "settings", settings)
    authentication_module = importlib.import_module("lenapi.authentication")
    monkeypatch.setattr(
        authentication_module,
        "install_default_authentication",
        lambda _app, _settings: None,
    )

    inherited = LENAPI(database=Database("sqlite://"), security_storage="memory")
    assert inherited.docs_url == "/private-docs/"
    assert inherited.openapi_url == "/schema.json"
    assert inherited.max_request_body_size == 4096
    assert inherited.request_body_timeout == 3.0
    assert inherited.media_root == tmp_path.resolve()
    assert inherited.media_url == "/assets/"
    assert inherited.docs_cookie_secure is True
    inherited.database.close()

    overridden = LENAPI(
        database=Database("sqlite://"),
        security_storage="memory",
        docs_url=None,
        openapi_url=None,
        max_request_body_size=None,
        request_body_timeout=None,
        media_root=None,
        media_url=None,
        docs_cookie_secure=None,
    )
    assert overridden.docs_url is None
    assert overridden.openapi_url is None
    assert overridden.max_request_body_size is None
    assert overridden.request_body_timeout is None
    assert overridden.media_root is None
    assert overridden.media_url is None
    assert overridden.docs_cookie_secure is None
    overridden.database.close()
