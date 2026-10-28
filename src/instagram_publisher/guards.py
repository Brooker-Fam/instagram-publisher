"""Reject unauthorized requests before Starlette parses multipart uploads."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from instagram_publisher.security import valid_admin_key


class RequestGuard:
    def __init__(self, app, admin_key: str, upload_limit: int):
        self.app = app
        self.admin_key = admin_key
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        if path.startswith("/api/"):
            auth = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, value = auth.partition(" ")
            if scheme.lower() != "bearer" or not valid_admin_key(value, self.admin_key):
                response = JSONResponse(
                    {"detail": "A valid admin Bearer token is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
                return await response(scope, receive, send)
        limit = self.upload_limit if path == "/api/assets" else 1_000_000
        length = headers.get(b"content-length", b"")
        if length and (not length.isdigit() or int(length) > limit):
            return await JSONResponse({"detail": "Request body exceeds the upload limit"}, status_code=413)(
                scope, receive, send
            )
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise HTTPException(413, "Request body exceeds the upload limit")
            return message

        await self.app(scope, bounded_receive, send)
