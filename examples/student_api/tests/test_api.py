"""HTTP-level pytest coverage for the project's actual main.app."""

import asyncio
import json

from main import app


def api_request(path, *, method="GET", payload=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = []
    if payload is not None:
        headers.extend([
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ])

    async def run():
        messages = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("198.51.100.10", 4321),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        response_headers = {key.decode().lower(): value.decode() for key, value in messages[0]["headers"]}
        return messages[0]["status"], response_headers, json.loads(messages[1]["body"])

    return asyncio.run(run())


def test_main_api_root_responds():
    status, _headers, payload = api_request("/")

    assert status == 200
    assert payload["message"] == "lenApi is running"


def test_main_api_validates_and_returns_request_body():
    status, _headers, payload = api_request(
        "/home",
        method="POST",
        payload={"person_name": "Ifeanyi", "course": "Python"},
    )

    assert status == 200
    assert payload == {"person_name": "Ifeanyi", "course": "Python", "classroom_id": None}


def test_main_api_rejects_invalid_request_body():
    status, _headers, payload = api_request(
        "/home", method="POST", payload={"person_name": "Ifeanyi"}
    )

    assert status == 422
    assert payload == {"error": "Invalid request"}


def test_main_api_openapi_includes_async_crud_routes():
    status, _headers, payload = api_request("/openapi.json")

    assert status == 200
    assert "/async/students" in payload["paths"]
    assert "/async/students/{student_id}" in payload["paths"]
    assert "post" in payload["paths"]["/async/students"]
    assert "put" in payload["paths"]["/async/students/{student_id}"]
    assert "delete" in payload["paths"]["/async/students/{student_id}"]
