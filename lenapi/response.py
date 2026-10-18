"""HTTP response types."""

import json
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


class HTTPException(Exception):
    """Raise an HTTP error from a route or framework helper."""

    def __init__(self, status_code: int, detail: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
