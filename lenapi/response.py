"""HTTP response types."""

import asyncio
import json
import os
import stat
from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Any
import uuid


def _json_default(value: Any):
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class Response:
    def __init__(
        self,
        content: bytes | str = b"",
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self.body = content.encode("utf-8") if isinstance(content, str) else content
        self.status_code = status_code
        self.headers = {"content-type": media_type, **(headers or {})}
        self._cookies: list[str] = []

    def set_cookie(
        self,
        key: str,
        value: str,
        *,
        max_age: int | None = None,
        expires: str | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
    ) -> None:
        """Add a Set-Cookie header to this response."""
        if not key or any(char in key for char in "=;, \t\r\n"):
            raise ValueError("Invalid cookie name")
        if samesite is not None and samesite.lower() not in {"lax", "strict", "none"}:
            raise ValueError("samesite must be 'lax', 'strict', 'none', or None")
        cookie = SimpleCookie()
        cookie[key] = value
        morsel = cookie[key]
        morsel["path"] = path
        if domain is not None:
            morsel["domain"] = domain
        if max_age is not None:
            morsel["max-age"] = str(max_age)
        if expires is not None:
            morsel["expires"] = expires
        if secure:
            morsel["secure"] = True
        if httponly:
            morsel["httponly"] = True
        if samesite is not None:
            morsel["samesite"] = samesite.capitalize()
        self._cookies.append(morsel.OutputString())

    def delete_cookie(self, key: str, *, path: str = "/", domain: str | None = None) -> None:
        """Expire a cookie by adding a matching deletion cookie."""
        self.set_cookie(key, "", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT", path=path, domain=domain)

    async def __call__(self, scope: dict, receive, send) -> None:
        headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in self.headers.items()]
        headers.extend((b"set-cookie", value.encode("latin-1")) for value in self._cookies)
        headers.append((b"content-length", str(len(self.body)).encode("ascii")))
        await send({"type": "http.response.start", "status": self.status_code, "headers": headers})
        await send({"type": "http.response.body", "body": self.body})


class JSONResponse(Response):
    def __init__(self, content: Any, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> None:
        body = json.dumps(content, ensure_ascii=False, separators=(",", ":"), default=_json_default).encode("utf-8")
        super().__init__(body, status_code=status_code, headers=headers, media_type="application/json")


class FileResponse(Response):
    """Stream a local file to the client in bounded chunks."""

    def __init__(
        self,
        path,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str = "application/octet-stream",
        chunk_size: int = 64 * 1024,
    ) -> None:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        super().__init__(b"", status_code=status_code, headers=headers, media_type=media_type)
        self.path = os.fspath(path)
        self.chunk_size = chunk_size

    async def __call__(self, scope: dict, receive, send) -> None:
        # O_NOFOLLOW prevents a final-component symlink swap between the
        # media-root containment check and opening the file where supported.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = None
        try:
            fd = await asyncio.to_thread(os.open, self.path, flags)
            metadata = await asyncio.to_thread(os.fstat, fd)
            if not stat.S_ISREG(metadata.st_mode):
                await asyncio.to_thread(os.close, fd)
                fd = None
                await Response(b"Not found", status_code=404)(scope, receive, send)
                return
        except OSError:
            if fd is not None:
                await asyncio.to_thread(os.close, fd)
            await Response(b"Not found", status_code=404)(scope, receive, send)
            return

        headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in self.headers.items()
        ]
        headers.extend((b"set-cookie", value.encode("latin-1")) for value in self._cookies)
        headers.append((b"content-length", str(metadata.st_size).encode("ascii")))
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": headers})
            remaining = metadata.st_size
            while remaining > 0:
                chunk = await asyncio.to_thread(os.read, fd, min(self.chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
            if metadata.st_size == 0 or remaining == 0:
                if metadata.st_size == 0:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            elif remaining > 0:
                # A file truncated during transfer: end the stream cleanly.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await asyncio.to_thread(os.close, fd)


class HTTPException(Exception):
    """Raise an HTTP error from a route or framework helper."""

    def __init__(self, status_code: int, detail: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
