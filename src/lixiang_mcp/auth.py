from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import AuthConfig, Settings
from .models import Principal, ServiceError

current_principal: ContextVar[Principal] = ContextVar("principal")


def principal() -> Principal:
    try:
        return current_principal.get()
    except LookupError:
        raise ServiceError("authentication_required") from None


class BackendAuth:
    """Backend credentials map to server-side grants; no OAuth passthrough."""

    def __init__(self, app: ASGIApp, config: AuthConfig, settings: Settings) -> None:
        self.app, self.config, self.settings = app, config, settings
        hashes = [c.token_sha256 for c in config.credentials]
        if len(hashes) != len(set(hashes)):
            raise ValueError("duplicate_backend_credential")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # Exact configured hostnames with optional port; no trusting X-Forwarded-* identity.
        host = headers.get("host", "").split(":", 1)[0]
        origin = headers.get("origin")
        if host not in self.settings.allowed_hosts or (
            origin is not None and origin not in self.settings.allowed_origins
        ):
            await JSONResponse({"error": "untrusted_host_or_origin"}, 403)(scope, receive, send)
            return
        if scope["path"] == "/healthz" and scope["method"] == "GET":
            await JSONResponse({"status": "ok", "backend": self.settings.backend})(
                scope, receive, send
            )
            return
        authorization = headers.getlist("authorization")
        credential = authorization[0] if len(authorization) == 1 else ""
        actor = None
        if credential.startswith("Bearer ") and 32 <= len(credential[7:]) <= 512:
            digest = hashlib.sha256(credential[7:].encode()).hexdigest()
            for entry in self.config.credentials:
                if hmac.compare_digest(entry.token_sha256, digest):
                    actor = entry.principal
        if actor is None:
            await JSONResponse(
                {"error": "authentication_required"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
            return
        # Bound request size, including chunked bodies, before passing it to the SDK.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 16384:
                await JSONResponse({"error": "request_too_large"}, 413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        consumed = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        token = current_principal.set(actor)
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            current_principal.reset(token)
