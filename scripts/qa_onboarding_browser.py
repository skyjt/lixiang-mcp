#!/usr/bin/env python3
"""Headless user-journey check. Uses only synthetic vendor HTTP and loopback UI requests."""

import argparse
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def serve(directory):
    import httpx
    import uvicorn

    from lixiang_mcp.cloud.profiles import builtin_profile
    from lixiang_mcp.private_files import private_write, secret_json
    from lixiang_mcp.safe_logging import configure_logging
    from lixiang_mcp.setup.storage import SetupStore
    from lixiang_mcp.setup.web import create_setup_app
    from lixiang_mcp.setup.wizard import Wizard

    fixtures = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/cloud/conftest.py"))
    config = fixtures["config"].__wrapped__().model_copy(update={"profile": builtin_profile()})
    cloud = fixtures["SyntheticCloud"](config)
    original = cloud.handle

    async def synthetic(request):
        cloud.login_redirect = (
            None
            if (directory / "official-completed").exists()
            else config.profile.redirect_uri + "?require=SMS_CODE"
        )
        response = await original(request)
        (directory / "counts.json").write_text(
            json.dumps(
                {
                    "login_count": cloud.login_count,
                    "control_posts": len(cloud.sends),
                }
            )
        )
        return response

    async def deny_network(*args, **kwargs):
        raise AssertionError("Live vendor HTTP is forbidden")

    httpx.AsyncHTTPTransport.handle_async_request = deny_network
    cloud.handle = synthetic
    store = SetupStore(directory / "wizard")
    wizard = Wizard(store, transport_factory=cloud.transport)
    token = "browser-fixture-" + "x" * 48
    private_write(
        directory / "material.json",
        secret_json(
            config.accounts[0].model_dump(
                include={
                    "device_id",
                    "hac_key_hex",
                    "key_id",
                    "app_token",
                }
            )
        ),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    app = create_setup_app(wizard, token, port=port)
    private_write(directory / "url", f"http://127.0.0.1:{port}/#{token}".encode())
    configure_logging()
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=False, proxy_headers=False)
    )
    try:
        server.run(sockets=[listener])
    finally:
        (directory / "counts.json").write_text(
            json.dumps(
                {
                    "login_count": cloud.login_count,
                    "control_posts": len(cloud.sends),
                }
            )
        )


def check(executable):
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="lixiang-browser-") as temporary:
        directory = Path(temporary)
        with (directory / "server.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, __file__, "--serve", str(directory)], stdout=log, stderr=log
            )
            try:
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError("synthetic_ui_server_failed")
                    if (directory / "url").exists():
                        url = (directory / "url").read_text()
                        target = urlsplit(url)
                        try:
                            with socket.create_connection(("127.0.0.1", target.port), timeout=1):
                                break
                        except OSError:
                            pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("synthetic_ui_startup_timeout")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=executable, headless=True)
                    page = browser.new_page()
                    page.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if urlsplit(route.request.url).hostname == "127.0.0.1"
                            else route.abort()
                        ),
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    expect(page.locator("#phase")).to_have_text("开始本机接入")
                    page.get_by_label("手机号", exact=True).fill("+" + "0" * 7)
                    page.get_by_label("密码", exact=True).fill("synthetic-password")
                    page.get_by_role("button", name="登录并继续", exact=True).click()
                    expect(page.locator("#phase")).to_have_text("需要本人完成官方验证")
                    official = urlsplit(page.locator("#official-link").get_attribute("href"))
                    assert official.netloc == "account.lixiang.com"
                    assert parse_qs(official.query)["device_id"][0]
                    # Simulate the account-side outcome; never open the real official site.
                    (directory / "official-completed").touch()
                    page.get_by_role("button", name="我已完成官方验证，继续").click()
                    expect(page.locator("#phase")).to_have_text("账号已登录，签名材料尚未就绪")
                    page.locator("#material").set_input_files(directory / "material.json")
                    expect(page.locator("#phase")).to_have_text("需要对齐设备身份")
                    page.get_by_role("button", name="使用此签名设备重新验证").click()
                    expect(page.locator("#phase")).to_have_text("请选择要接入的车辆")
                    choices = page.locator("#choices input[type=checkbox]")
                    assert choices.count() == 2 and not choices.nth(0).is_checked()
                    choices.nth(1).check()
                    # Wait through a status poll; it must not clear an in-progress selection.
                    page.wait_for_timeout(1700)
                    assert choices.nth(1).is_checked()
                    page.get_by_role("button", name="保存所选车辆，保持只读").click()
                    expect(page.locator("#phase")).to_have_text("只读配置已生成")
                    settings_path = Path(page.locator("#config").text_content())
                    settings = json.loads(settings_path.read_text())
                    protocol = json.loads(Path(settings["vehicle_secrets_file"]).read_text())
                    grants = json.loads(Path(settings["auth_file"]).read_text())
                    assert len(protocol["vehicles"]) == 1
                    assert not settings["enable_control"] and not protocol["allow_real_control"]
                    assert grants["credentials"][0]["principal"]["scopes"] == ["vehicle:read"]
                    assert not errors and not urlsplit(page.url).fragment
                    browser.close()
            finally:
                process.terminate()
                process.wait(timeout=15)
        counts = json.loads((directory / "counts.json").read_text())
        assert counts == {"login_count": 3, "control_posts": 0}
        print(
            "PASS: local browser login, official-verification return, signing-device reauth, "
            "vehicle selection, readonly export; no live vendor requests"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-executable")
    parser.add_argument("--serve", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.serve:
        serve(args.serve)
    elif args.browser_executable:
        check(args.browser_executable)
    else:
        parser.error("Pass an installed Chromium executable; this script never downloads a browser")
