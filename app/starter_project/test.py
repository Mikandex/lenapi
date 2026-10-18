"""Starter application smoke test."""

from main import app


def test_root_route_is_registered():
    assert any(route.path == "/" and "GET" in route.methods for route in app.routes)
