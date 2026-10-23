"""Convenient access to an incoming ASGI request."""

import asyncio
from contextvars import ContextVar, Token
import json
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
import os
from pathlib import Path
import re
import time
from types import UnionType
from typing import get_args, get_origin, get_type_hints, Union
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

    def __init__(
        self, filename: str, content_type: str, data: bytes, *, media_root: str | Path | None = None
    ) -> None:
        self.filename = Path(filename.replace("\\", "/")).name or "upload"
        self.content_type = content_type
        self.size = len(data)
        self._data = data
        self.media_root = Path(media_root).expanduser().resolve() if media_root is not None else None

    async def read(self) -> bytes:
        return self._data

    def _image_extension(self) -> str:
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
        return detected

    @staticmethod
    def _safe_name(filename: str) -> str:
        safe_name = Path(filename.replace("\\", "/")).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("Invalid upload filename")
        return safe_name

    @staticmethod
    def _directory(root: str | Path, directory: str | Path) -> Path:
        root = Path(root).expanduser().resolve()
        relative = Path(directory)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Upload directory must be relative to MEDIA_ROOT")
        destination = (root / relative).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("Upload directory must stay inside MEDIA_ROOT")
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    async def _write(self, directory: str | Path, safe_name: str) -> str:
        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = (root / safe_name).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("Upload destination must stay inside its directory")

        def write_exclusive() -> None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(destination, flags, 0o644)
            except FileExistsError as exc:
                raise ValueError("A file with this filename already exists") from exc
            try:
                with os.fdopen(fd, "wb") as upload_file:
                    upload_file.write(self._data)
            except Exception:
                destination.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(write_exclusive)
        return safe_name

    async def save(
        self,
        directory: str | Path = "",
        *,
        type: str | None = "image",
        keep_filename: bool = False,
    ) -> str:
        """Save under MEDIA_ROOT; return the stored filename.

        By default uploads are validated as images and get a generated name.
        Set ``type`` to an extension such as ``"pdf"`` or ``"doc"`` to allow
        that document type, or ``None`` to allow any file type. Set
        ``keep_filename=True`` to retain the sanitized uploaded filename.
        Existing files are never overwritten.
        """
        if self.media_root is None:
            raise RuntimeError("File saving requires LENAPI MEDIA_ROOT configuration")
        if not isinstance(keep_filename, bool):
            raise TypeError("keep_filename must be a boolean")

        extension: str
        if type == "image":
            extension = self._image_extension()
        elif type is None:
            suffix = Path(self.filename).suffix.lower()
            extension = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
        elif isinstance(type, str) and re.fullmatch(r"[a-zA-Z0-9]{1,10}", type):
            selected = type.lower().lstrip(".")
            filename_suffix = Path(self.filename).suffix.lower()
            allowed_suffixes = {".doc", ".docx"} if selected == "doc" else {f".{selected}"}
            if filename_suffix not in allowed_suffixes:
                raise ValueError(f"Uploaded filename must use one of: {', '.join(sorted(allowed_suffixes))}")
            if selected == "pdf" and not self._data.startswith(b"%PDF-"):
                raise ValueError("Uploaded file does not contain a PDF signature")
            if selected in {"doc", "docx"} and not (
                self._data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
                or (filename_suffix == ".docx" and self._data.startswith(b"PK\x03\x04"))
            ):
                raise ValueError("Uploaded file does not contain a Word document signature")
            extension = filename_suffix
        else:
            raise ValueError("type must be an extension such as 'pdf', 'doc', 'image', or None")

        directory_path = self._directory(self.media_root, directory)
        safe_name = self._safe_name(self.filename) if keep_filename else f"{uuid.uuid4().hex}{extension}"
        return await self._write(directory_path, safe_name)

    async def save_image(self, directory: str | Path, *, filename: str | None = None) -> str:
        """Validate common image signatures and save safely; return the filename."""
        detected = self._image_extension()
        if filename:
            requested = self._safe_name(filename)
            suffix = Path(requested).suffix.lower()
            valid_suffixes = {".jpeg", ".jpg"} if detected == ".jpg" else {detected}
            if suffix not in valid_suffixes:
                raise ValueError("Image filename extension does not match its image content")
            safe_name = requested
        else:
            safe_name = f"{uuid.uuid4().hex}{detected}"
        return await self._write(directory, safe_name)


def upload_file_shape(annotation) -> str | None:
    """Return ``single`` or ``multiple`` for UploadFile annotations."""
    if annotation is UploadFile:
        return "single"
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        shapes = {upload_file_shape(arg) for arg in args if arg is not type(None)}
        shapes.discard(None)
        return next(iter(shapes)) if len(shapes) == 1 else None
    if origin in (list, tuple):
        if any(upload_file_shape(arg) == "single" for arg in args):
            return "multiple"
    if isinstance(annotation, str):
        value = annotation.replace(" ", "").strip("'\"")
        if value.endswith("UploadFile"):
            return "single"
        if value.startswith(("list[", "List[", "tuple[", "Tuple[")) and "UploadFile" in value:
            return "multiple"
        if value.startswith(("Optional[", "Union[")) and "UploadFile" in value:
            return "single"
    return None


def endpoint_type_hints(endpoint) -> dict:
    """Resolve postponed annotations when possible, preserving safe fallbacks."""
    try:
        return get_type_hints(endpoint)
    except (NameError, TypeError, AttributeError):
        return {}


class Request:
    def __init__(self, scope: dict, receive, *, max_body_size: int | None = 10 * 1024 * 1024,
                 body_timeout: float | None = 30.0, media_root: str | Path | None = None) -> None:
        self.scope = scope
        self._receive = receive
        self.max_body_size = max_body_size
        self.body_timeout = body_timeout
        self.media_root = Path(media_root).expanduser().resolve() if media_root is not None else None
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
                value = UploadFile(filename, part.get_content_type(), data, media_root=self.media_root)
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
