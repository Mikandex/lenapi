"""Coverage for the per-route IP cooldown (API throttle)."""

import asyncio
import json

import pytest

from lenapi import Application


def asgi_request(app, path, *, client_ip="127.0.0.1"):
    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": (client_ip, 12345),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        headers = {key.decode().lower(): value.decode() for key, value in messages[0]["headers"]}
        return messages[0]["status"], headers, json.loads(messages[1]["body"])

    return asyncio.run(run())


def test_api_throttle_blocks_same_ip_during_cooldown():
    app = Application(security_storage="memory")

    @app.get("/limited", api_flag=True, cool_time=300)
    def limited():
        return {"ok": True}

    first = asgi_request(app, "/limited")
    second = asgi_request(app, "/limited")

    assert first[0] == 200
    assert first[2] == {"ok": True}
    assert second[0] == 429
    assert second[1]["retry-after"].isdigit()
    assert second[2] == {"detail": "Too many requests", "cool_time": 300}


def test_api_throttle_tracks_each_ip_independently():
    app = Application(security_storage="memory")

    @app.get("/limited", api_flag=True, cool_time=300)
    def limited():
        return {"ok": True}

    assert asgi_request(app, "/limited", client_ip="192.0.2.1")[0] == 200
    assert asgi_request(app, "/limited", client_ip="192.0.2.2")[0] == 200


def test_api_throttle_rejects_non_positive_cool_time():
    app = Application(security_storage="memory")
    with pytest.raises(ValueError, match="positive"):
        @app.get("/limited", api_flag=True, cool_time=-1)
        def limited():
            return {"ok": True}
