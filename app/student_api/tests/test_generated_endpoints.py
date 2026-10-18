"""Generated endpoint registration and OpenAPI coverage."""

from importlib import import_module

import pytest

from lenapi.openapi import build_openapi

APP_IMPORT_STRING = 'main:app'
HTTP_ENDPOINTS = [('delete', '/async/students/{student_id}'), ('delete', '/students/batch'), ('delete', '/students/{student_id}'), ('get', '/'), ('get', '/async/students'), ('get', '/async/students/{student_id}'), ('get', '/auth/profile'), ('get', '/classrooms'), ('get', '/students'), ('get', '/students/exists'), ('get', '/students/filter'), ('get', '/students/lookup'), ('get', '/students/names'), ('get', '/students/search'), ('get', '/students/summary'), ('get', '/students/{student_id}/classroom'), ('post', '/async/students'), ('post', '/auth/refresh'), ('post', '/auth/register'), ('post', '/auth/token'), ('post', '/classrooms'), ('post', '/home'), ('post', '/photos'), ('post', '/students'), ('post', '/students/batch'), ('post', '/webhooks/events'), ('put', '/async/students/{student_id}'), ('put', '/students/batch'), ('put', '/students/{student_id}')]
WEBSOCKET_ENDPOINTS = []


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
