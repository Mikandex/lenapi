"""Convenient access to an incoming ASGI request."""

import asyncio
from contextvars import ContextVar, Token
import json
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from pathlib import Path
import time
import uuid
from urllib.parse import parse_qs

from .response import HTTPException


_current_request: ContextVar["Request | None"] = ContextVar("lenapi_current_request", default=None)


def set_current_request(request: "Request") -> Token:
    """Make the active request available to framework-managed callbacks."""
    return _current_request.set(request)


def reset_current_request(token: Token) -> None:
    _current_request.reset(token)


def get_current_request() -> "Request":
    """Return the active request inside a dispatched route."""
    request = _current_request.get()
    if request is None:
        raise RuntimeError("There is no active LenApi request in this context")
    return request


class UploadFile:
    """An uploaded multipart file held in memory for a bounded request."""

    def __init__(self, filename: str, content_type: str, data: bytes) -> None:
        self.filename = Path(filename).name
        self.content_type = content_type
        self.size = len(data)
        self._data = data

    async def read(self) -> bytes:
        return self._data

    async def save_image(self, directory: str | Path, *, filename: str | None = None) -> str:
        """Validate common image signatures and save safely; return the filename."""
        image_types = (
            (self._data.startswith(b"\xff\xd8\xff"), ".jpg", {"image/jpeg", "image/jpg"}),
            (self._data.startswith(b"\x89PNG\r\n\x1a\n"), ".png", {"image/png"}),
            (self._data.startswith((b"GIF87a", b"GIF89a")), ".gif", {"image/gif"}),
            (self._data.startswith(b"RIFF") and self._data[8:12] == b"WEBP", ".webp", {"image/webp"}),
            (self._data.startswith(b"BM"), ".bmp", {"image/bmp", "image/x-ms-bmp"}),
        )
        detected = next((extension for matches, extension, _types in image_types if matches), None)
        allowed_types = next((types for matches, _extension, types in image_types if matches), set())
        if detected is None or self.content_type.lower() not in allowed_types:
            raise ValueError("Uploaded file is not a supported image or its content type does not match")
        if filename:
            requested = Path(filename).name
            suffix = Path(requested).suffix.lower()
            valid_suffixes = {".jpeg", ".jpg"} if detected == ".jpg" else {detected}
            if suffix not in valid_suffixes:
                raise ValueError("Image filename extension does not match its image content")
            safe_name = requested
        else:
            safe_name = f"{uuid.uuid4().hex}{detected}"
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("Invalid image filename")
        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = (root / safe_name).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("Image destination must stay inside its upload directory")
        await asyncio.to_thread(destination.write_bytes, self._data)
        return safe_name


class Request:
    def __init__(self, scope: dict, receive, *, max_body_size: int | None = 10 * 1024 * 1024,
                 body_timeout: float | None = 30.0) -> None:
        self.scope = scope
        self._receive = receive
        self.max_body_size = max_body_size
        self.body_timeout = body_timeout
        self._body: bytes | None = None

    @property
    def method(self) -> str:
        return self.scope["method"]

    @property
    def path(self) -> str:
        return self.scope["path"]

    @property
    def client_ip(self) -> str | None:
        """Client address from the ASGI connection; proxy headers are not trusted."""
        client = self.scope.get("client")
        return client[0] if client else None

    @property
    def path_params(self) -> dict[str, object]:
        return self.scope.get("path_params", {})

    @property
    def headers(self) -> dict[str, str]:
        """Request headers keyed by lowercase names."""
        return {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in self.scope.get("headers", [])
        }

    @property
    def cookies(self) -> dict[str, str]:
        """Incoming cookies parsed from the Cookie request header."""
        cookie = SimpleCookie()
        cookie.load(self.headers.get("cookie", ""))
        return {name: morsel.value for name, morsel in cookie.items()}

    @property
    def query_params(self) -> dict[str, str | list[str]]:
        parsed = parse_qs(self.scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}

    async def body(self) -> bytes:
        if self._body is None:
            chunks = []
            size = 0
            deadline = time.monotonic() + self.body_timeout if self.body_timeout is not None else None
            while True:
                try:
                    if deadline is None:
                        message = await self._receive()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        message = await asyncio.wait_for(self._receive(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise HTTPException(408, "Request body timed out") from exc
                if message["type"] == "http.disconnect":
                    break
                chunk = message.get("body", b"")
                size += len(chunk)
                if self.max_body_size is not None and size > self.max_body_size:
                    raise HTTPException(413, "Request body too large")
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            self._body = b"".join(chunks)
        return self._body

    async def json(self):
        return json.loads((await self.body()).decode("utf-8"))

    async def form(self) -> dict[str, str | UploadFile | list[str | UploadFile]]:
        """Parse URL-encoded or multipart form data, including file uploads."""
        content_type = self.headers.get("content-type", "")
        if content_type.lower().startswith("application/x-www-form-urlencoded"):
            parsed = parse_qs((await self.body()).decode("utf-8"), keep_blank_values=True)
            return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}
        if not content_type.lower().startswith("multipart/form-data"):
            raise HTTPException(415, "Expected form data")
        raw = await self.body()
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + raw
        )
        if not message.is_multipart():
            raise HTTPException(400, "Invalid multipart form data")
        values: dict[str, str | UploadFile | list[str | UploadFile]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            data = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            value: str | UploadFile
            if filename is not None:
                value = UploadFile(filename, part.get_content_type(), data)
            else:
                charset = part.get_content_charset() or "utf-8"
                try:
                    value = data.decode(charset)
                except (LookupError, UnicodeDecodeError) as exc:
                    raise HTTPException(400, "Invalid form field encoding") from exc
            if name in values:
                existing = values[name]
                if not isinstance(existing, list):
                    values[name] = [existing, value]
                else:
                    existing.append(value)
            else:
                values[name] = value
        return values
