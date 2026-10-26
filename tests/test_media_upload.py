"""Automated coverage for multipart uploads and media serving."""

import asyncio
import json

from lenapi import Application, AsyncDatabase, Database, File, Model, StringField, UploadFile
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

    @app.post("/photos")
    async def upload_photo(image: UploadFile = File(...)):
        filename = await image.save("photos")
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
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return messages[0]["status"], response_headers, response_body


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


def test_upload_with_existing_filename_does_not_replace_file(tmp_path):
    destination = tmp_path / "avatar.png"
    original = b"existing image contents"
    destination.write_bytes(original)
    upload = UploadFile("avatar.png", "image/png", PNG_BYTES)

    try:
        asyncio.run(upload.save_image(tmp_path, filename="avatar.png"))
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("save_image should refuse to overwrite an existing path")

    assert destination.read_bytes() == original


def test_media_serving_rejects_paths_outside_media_root(tmp_path):
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("private")
    app = make_app(tmp_path)

    status, _headers, _body = asgi_request(app, "/media/../outside.txt")

    assert status == 404


def test_media_serves_raster_images_inline_with_nosniff(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "safe.png").write_bytes(PNG_BYTES)

    status, headers, _body = asgi_request(app, "/media/safe.png")

    assert status == 200
    assert headers[b"content-type"] == b"image/png"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert b"content-disposition" not in headers


def test_media_files_are_streamed_without_eagerly_loading_contents(tmp_path):
    from lenapi.response import FileResponse

    app = make_app(tmp_path)
    payload = b"x" * (160 * 1024)
    (tmp_path / "large.bin").write_bytes(payload)

    response = app._media_response("large.bin")
    assert isinstance(response, FileResponse)
    assert response.body == b""

    status, headers, body = asgi_request(app, "/media/large.bin")
    assert status == 200
    assert headers[b"content-length"] == str(len(payload)).encode()
    assert body == payload


def test_media_downloads_active_content_instead_of_serving_inline(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "page.html").write_text("<script>alert(1)</script>")
    (tmp_path / "vector.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")

    for path, expected_type in (
        ("/media/page.html", b"text/html"),
        ("/media/vector.svg", b"image/svg+xml"),
    ):
        status, headers, _body = asgi_request(app, path)
        assert status == 200
        assert headers[b"content-type"].startswith(expected_type)
        assert headers[b"content-disposition"] == b"attachment"
        assert headers[b"x-content-type-options"] == b"nosniff"


def test_openapi_documents_multipart_file_field(tmp_path):
    app = make_app(tmp_path)
    status, _headers, response_body = asgi_request(app, "/openapi.json")

    assert status == 200
    upload_body = json.loads(response_body)["paths"]["/photos"]["post"]["requestBody"]
    assert upload_body["required"] is True
    schema = upload_body["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["image"] == {"type": "string", "format": "binary"}


def test_upload_parameter_receives_file_without_manual_openapi_schema(tmp_path):
    app = make_app(tmp_path)
    boundary, body = multipart_image()
    status, _headers, response_body = asgi_request(
        app,
        "/photos",
        method="POST",
        body=body,
        headers=[(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    )

    assert status == 200
    assert json.loads(response_body)["image"].startswith("photos/")
    image_path = tmp_path / json.loads(response_body)["image"]
    assert image_path.is_file()
    assert image_path.read_bytes() == PNG_BYTES


def test_file_marker_requires_upload(tmp_path):
    app = make_app(tmp_path)
    status, _headers, response_body = asgi_request(
        app,
        "/photos",
        method="POST",
        body=(
            b'--test-boundary\r\nContent-Disposition: form-data; name="other"'
            b'\r\n\r\nx\r\n--test-boundary--\r\n'
        ),
        headers=[(b"content-type", b"multipart/form-data; boundary=test-boundary")],
    )

    assert status == 422
    assert json.loads(response_body)["detail"] == "Missing uploaded file: image"


def test_save_can_preserve_sanitized_original_filename(tmp_path):
    upload = UploadFile("../portrait.png", "image/png", PNG_BYTES, media_root=tmp_path)
    filename = asyncio.run(upload.save("photos", keep_filename=True))

    assert filename == "portrait.png"
    assert (tmp_path / "photos" / filename).read_bytes() == PNG_BYTES
    try:
        asyncio.run(upload.save("photos", keep_filename=True))
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("save must not overwrite a file with the same name")


def test_save_accepts_selected_pdf_or_any_file(tmp_path):
    pdf = UploadFile("terms.pdf", "application/pdf", b"%PDF-1.7\nbody", media_root=tmp_path)
    stored_pdf = asyncio.run(pdf.save("documents", type="pdf", keep_filename=True))
    assert stored_pdf == "terms.pdf"
    assert (tmp_path / "documents" / stored_pdf).read_bytes().startswith(b"%PDF-")

    arbitrary = UploadFile("payload.bin", "application/octet-stream", b"payload", media_root=tmp_path)
    stored_any = asyncio.run(arbitrary.save("misc", type=None))
    assert stored_any.endswith(".bin")
    assert (tmp_path / "misc" / stored_any).read_bytes() == b"payload"


def test_file_upload_rejects_images_but_file_image_accepts_both(tmp_path):
    image = UploadFile("sample.png", "image/png", PNG_BYTES, media_root=tmp_path)
    try:
        asyncio.run(image.save("files", type="file"))
    except ValueError as exc:
        assert "does not accept image uploads" in str(exc)
    else:
        raise AssertionError("FileField uploads must reject image files")

    image_name = asyncio.run(image.save("mixed", type="file_image"))
    assert image_name.endswith(".png")
    assert (tmp_path / "mixed" / image_name).read_bytes() == PNG_BYTES

    document = UploadFile("notes.pdf", "application/pdf", b"%PDF-1.7", media_root=tmp_path)
    document_name = asyncio.run(document.save("files", type="file"))
    assert document_name.endswith(".pdf")
    assert (tmp_path / "files" / document_name).read_bytes() == b"%PDF-1.7"


def test_upload_max_size_is_enforced_before_writing(tmp_path):
    image = UploadFile("sample.png", "image/png", PNG_BYTES, media_root=tmp_path)
    try:
        asyncio.run(image.save("photos", max_size=4))
    except ValueError as exc:
        assert "maximum size" in str(exc)
    else:
        raise AssertionError("Upload size limit should be enforced")
    assert not (tmp_path / "photos").exists()


def test_save_requires_configured_media_root_and_checks_file_type(tmp_path):
    without_root = UploadFile("x.png", "image/png", PNG_BYTES)
    try:
        asyncio.run(without_root.save("photos"))
    except RuntimeError as exc:
        assert "MEDIA_ROOT" in str(exc)
    else:
        raise AssertionError("save without MEDIA_ROOT must fail")

    wrong_pdf = UploadFile("not-a-pdf.pdf", "application/pdf", b"not a PDF", media_root=tmp_path)
    try:
        asyncio.run(wrong_pdf.save("documents", type="pdf"))
    except ValueError as exc:
        assert "PDF signature" in str(exc)
    else:
        raise AssertionError("save(type='pdf') must validate the PDF signature")


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
        async with db.session() as session:
            existing = await AsyncTestRecord.aget(
                name="before", session=session
            ).exists()
            missing = await AsyncTestRecord.aget(
                name="missing", session=session
            ).exists()
            all_records = await AsyncTestRecord.aget.all(session=session)
        listed = await asgi_request_async(app, "/async-records")
        updated = await asgi_request_async(app, "/async-records", method="PUT")
        deleted = await asgi_request_async(app, "/async-records", method="DELETE")
        return created, existing, missing, all_records, listed, updated, deleted

    try:
        created, existing, missing, all_records, listed, updated, deleted = asyncio.run(run())
        assert created[0] == 200
        assert json.loads(created[2])["name"] == "before"
        assert existing is True
        assert missing is False
        assert len(all_records) == 1
        assert json.loads(listed[2]) == [{"id": json.loads(created[2])["id"], "name": "before"}]
        assert json.loads(updated[2])["name"] == "after"
        assert json.loads(deleted[2]) == {"deleted": 1}
    finally:
        asyncio.run(db.close())
