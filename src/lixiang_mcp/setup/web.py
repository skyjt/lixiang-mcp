from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..models import ServiceError
from ..private_files import private_write
from ..safe_logging import configure_logging
from .storage import SetupStore
from .wizard import Action, Wizard


class LocalOnly:
    def __init__(self, app: ASGIApp, token: str, port: int) -> None:
        self.app, self.token = app, token
        self.host = f"127.0.0.1:{port}"
        self.origin = "http://" + self.host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def safe_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"default-src 'none'; script-src 'self'; style-src 'self'; "
                        b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                        b"form-action 'self'",
                    ),
                ]
            await send(message)

        headers = Headers(scope=scope)
        peer = scope.get("client")
        origin = headers.get("origin")
        if (
            peer is None
            or peer[0] != "127.0.0.1"
            or headers.get("host") != self.host
            or scope.get("query_string")
            or (origin is not None and origin != self.origin)
            or headers.get("sec-fetch-site") == "cross-site"
        ):
            await JSONResponse({"error": "local_browser_required"}, 403)(scope, receive, safe_send)
            return
        if scope["path"].startswith("/api/"):
            auth = headers.getlist("authorization")
            if len(auth) != 1 or not hmac.compare_digest(
                auth[0].encode(), ("Bearer " + self.token).encode()
            ):
                await JSONResponse({"error": "setup_authorization_required"}, 401)(
                    scope, receive, safe_send
                )
                return
            if scope["method"] == "POST" and (
                origin != self.origin
                or headers.get("content-type", "").split(";")[0] != "application/json"
            ):
                await JSONResponse({"error": "local_json_action_required"}, 403)(
                    scope, receive, safe_send
                )
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 16384:
                await JSONResponse({"error": "setup_request_too_large"}, 413)(
                    scope, receive, safe_send
                )
                return
            if not message.get("more_body", False):
                break
        consumed = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, safe_send)


def create_setup_app(wizard: Wizard, token: str, *, port: int = 8765) -> ASGIApp:
    async def status(request: Request) -> Response:
        return JSONResponse(wizard.status())

    async def action(request: Request) -> Response:
        try:
            command = Action.model_validate_json(await request.body())
            return JSONResponse(await wizard.act(command))
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid_setup_input"}, 400)
        except ServiceError as exc:
            return JSONResponse({"error": exc.code}, 409)
        except Exception:
            return JSONResponse({"error": "setup_action_failed"}, 500)

    async def asset(request: Request) -> Response:
        assets = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
        }
        name, media = assets[request.url.path]
        return Response(
            files("lixiang_mcp.setup").joinpath("assets", name).read_bytes(), media_type=media
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await wizard.close()

    app = Starlette(
        routes=[
            Route("/", asset),
            Route("/app.js", asset),
            Route("/style.css", asset),
            Route("/api/status", status),
            Route("/api/action", action, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    return LocalOnly(app, token, port)


def write_launch_file(directory: Path, token: str, port: int) -> Path:
    """Use the same private file entry point in the CLI and browser journey check."""
    launch = directory / "launch.html"
    url = f"http://127.0.0.1:{port}/#{token}"
    html = (
        '<!doctype html><meta name="referrer" content="no-referrer"><script>location.replace('
        + json.dumps(url)
        + ")</script>"
    )
    private_write(launch, html.encode())
    return launch


def main() -> None:
    parser = argparse.ArgumentParser(
        description="仅本机浏览器使用的理想账号接入向导；不属于 MCP 工具"
    )
    parser.add_argument("--state-dir", type=Path, default=Path("secrets/onboarding"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    configure_logging()
    try:
        wizard = Wizard(SetupStore(args.state_dir))
        token = secrets.token_urlsafe(32)
        launch = write_launch_file(wizard.store.directory, token, args.port)
        app = create_setup_app(wizard, token, port=args.port)
    except Exception:
        raise SystemExit(
            "setup_private_storage_invalid; check permissions, key and running process"
        ) from None
    print("本地接入向导；不要把此页面转发或通过公网代理。")
    print("请用本机浏览器打开私密文件：", launch)
    print("账号和密码只在本机输入；关闭向导按 Ctrl-C。")
    if not args.no_browser:
        # Only a file path goes to the browser launcher/process argv, never the capability.
        opener = threading.Timer(1.0, webbrowser.open, args=(launch.as_uri(),))
        opener.daemon = True
        opener.start()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        workers=1,
    )
