"""Automated coverage for multipart uploads and media serving."""

import asyncio
import json

from lenapi import Application, AsyncDatabase, Database, HTTPException, Model, Request, StringField, UploadFile
from lenapi.service import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-image-content"


class AsyncTestRecord(Model):
    name = StringField(max_length=80, unique=True)


def make_app(media_root):
    app = Application(
        database=Database("sqlite://"),
        media_root=media_root,
        media_url="/media/",
        security_storage="memory",
    )

    @app.post(
        "/photos",
        request_body={
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"image": {"type": "string", "format": "binary"}},
                        "required": ["image"],
                    }
                }
            },
        },
    )
    async def upload_photo(request: Request):
        form = await request.form()
        image = form.get("image")
        if not isinstance(image, UploadFile):
            raise HTTPException(422, "An image file is required")
        filename = await image.save_image(media_root / "photos")
        relative_path = f"photos/{filename}"
        return {"image": relative_path, "url": f"/media/{relative_path}"}

    return app


def asgi_request(app, path, *, method="GET", body=b"", headers=()):
    return asyncio.run(asgi_request_async(app, path, method=method, body=body, headers=headers))

async def asgi_request_async(app, path, *, method="GET", body=b"", headers=()):
    received = False
    messages = []

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
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    response_headers = dict(messages[0]["headers"])
    return messages[0]["status"], response_headers, messages[1]["body"]


def multipart_image():
    boundary = "test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="sample.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + PNG_BYTES + f"\r\n--{boundary}--\r\n".encode()
    return boundary, body


def test_upload_saves_image_and_media_url_serves_it(tmp_path):
    app = make_app(tmp_path)
    boundary, body = multipart_image()
    status, _headers, response_body = asgi_request(
        app,
        "/photos",
        method="POST",
        body=body,
        headers=[
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(body)).encode()),
        ],
    )

    assert status == 200
    payload = json.loads(response_body)
    assert payload["url"].startswith("/media/photos/")
    assert (tmp_path / payload["image"]).read_bytes() == PNG_BYTES

    image_status, image_headers, image_body = asgi_request(app, payload["url"])
    assert image_status == 200
    assert image_headers[b"content-type"] == b"image/png"
    assert image_body == PNG_BYTES


def test_upload_rejects_non_multipart_content_type(tmp_path):
    app = make_app(tmp_path)
    status, _headers, response_body = asgi_request(
        app,
        "/photos",
        method="POST",
        body=b"{}",
        headers=[(b"content-type", b"application/json")],
    )

    assert status == 415
    assert json.loads(response_body)["detail"] == "Expected form data"


def test_media_serving_rejects_paths_outside_media_root(tmp_path):
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("private")
    app = make_app(tmp_path)

    status, _headers, _body = asgi_request(app, "/media/../outside.txt")

    assert status == 404


def test_openapi_documents_multipart_file_field(tmp_path):
    app = make_app(tmp_path)
    status, _headers, response_body = asgi_request(app, "/openapi.json")

    assert status == 200
    upload_body = json.loads(response_body)["paths"]["/photos"]["post"]["requestBody"]
    schema = upload_body["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["image"] == {"type": "string", "format": "binary"}


def test_websocket_route_accepts_and_echoes_json():
    app = Application(database=Database("sqlite://"), security_storage="memory")

    @app.websocket("/ws/{room_id}")
    async def websocket_echo(websocket: WebSocket, room_id: int):
        await websocket.accept()
        payload = await websocket.receive_json()
        await websocket.send_json({"room_id": room_id, "payload": payload})

    async def run():
        incoming = iter([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": '{"message":"hello"}'},
        ])
        sent = []

        async def receive():
            return next(incoming)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "path": "/ws/12",
            "query_string": b"",
            "headers": [],
        }
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    assert sent[0] == {"type": "websocket.accept"}
    assert json.loads(sent[1]["text"]) == {"room_id": 12, "payload": {"message": "hello"}}
    assert sent[2]["type"] == "websocket.close"


def test_faker_service_generates_repeatable_data():
    import pytest

    pytest.importorskip("faker")
    from lenapi.service import Faker

    first = Faker("en_US", seed=42)
    second = Faker("en_US", seed=42)
    assert first.name() == second.name()


def test_async_session_is_injected_and_closed_per_request():
    class StubAsyncSession:
        closed = False

        async def close(self):
            self.closed = True

    session = StubAsyncSession()

    class StubAsyncDatabase:
        session_factory = staticmethod(lambda: session)

    app = Application(async_database=StubAsyncDatabase(), security_storage="memory")

    @app.get("/async-session")
    async def read_with_session(db: AsyncSession):
        return {"injected": db is session}

    status, _headers, body = asgi_request(app, "/async-session")

    assert status == 200
    assert json.loads(body) == {"injected": True}
    assert session.closed


def test_async_model_crud_methods(tmp_path):
    import pytest

    pytest.importorskip("aiosqlite")
    db = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'async-models.db'}")
    app = Application(async_database=db, security_storage="memory")

    @app.post("/async-records")
    async def create_record():
        record = await AsyncTestRecord.acreate(name="before")
        return {"id": record.id, "name": record.name}

    @app.get("/async-records")
    async def list_records():
        records = await AsyncTestRecord.afilter().order("_id").all()
        return [{"id": record.id, "name": record.name} for record in records]

    @app.put("/async-records")
    async def update_record():
        record = await AsyncTestRecord.aget(name="before")
        record.name = "after"
        await record.asave()
        return {"id": record.id, "name": record.name}

    @app.delete("/async-records")
    async def delete_records():
        deleted = await AsyncTestRecord.adelete(name="after")
        return {"deleted": deleted}

    async def run():
        async with db.engine.begin() as connection:
            await connection.run_sync(AsyncTestRecord.metadata.create_all)

        created = await asgi_request_async(app, "/async-records", method="POST")
        listed = await asgi_request_async(app, "/async-records")
        updated = await asgi_request_async(app, "/async-records", method="PUT")
        deleted = await asgi_request_async(app, "/async-records", method="DELETE")
        return created, listed, updated, deleted

    try:
        created, listed, updated, deleted = asyncio.run(run())
        assert created[0] == 200
        assert json.loads(created[2])["name"] == "before"
        assert json.loads(listed[2]) == [{"id": json.loads(created[2])["id"], "name": "before"}]
        assert json.loads(updated[2])["name"] == "after"
        assert json.loads(deleted[2]) == {"deleted": 1}
    finally:
        asyncio.run(db.close())
