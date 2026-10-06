"""Synthetic contracts only. A guard makes an accidental real HTTP call fail the suite."""

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from lixiang_mcp.cloud.api import RESULT, SEND, VSS
from lixiang_mcp.cloud.config import CloudConfig
from lixiang_mcp.cloud.signals import BOOLS, LOCATION_PATH, NUMBERS
from lixiang_mcp.models import now


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    async def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in protocol contract tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)


@pytest.fixture
def config():
    profile = {
        key: f"fixture-{key}"
        for key in (
            "client_id",
            "login_audience",
            "login_scope",
            "vehicles_audience",
            "vss_audience",
            "mesh_audience",
            "vat_audience",
            "login_app_version",
            "sdk_version",
            "sign_app_version",
            "login_user_agent",
            "api_user_agent",
        )
    }
    profile["redirect_uri"] = "fixture://auth/callback"
    accounts, vehicles = [], []
    for index, account in enumerate(("account-a", "account-b")):
        accounts.append(
            {
                "account_id": account,
                "phone": "+" + "0" * 7,
                "password": "synthetic-password",
                "device_id": f"synthetic-device-{account}",
                "key_id": f"synthetic-key-{account}",
                "hac_key_hex": bytes(range(32)).hex(),
                "app_token": f"synthetic-app-{account}",
            }
        )
        for offset in range(2):
            vehicles.append(
                {
                    "vehicle_id": f"car-{index}-{offset}",
                    "account_id": account,
                    "vin": "TEST" + str(index * 10 + offset).zfill(13),
                    "label": "Synthetic vehicle",
                    "model_label": "Synthetic model",
                    "model_id": "fixture-model",
                    "climate_supported": True,
                    "location_supported": True,
                }
            )
    return CloudConfig.model_validate(
        {"profile": profile, "accounts": accounts, "vehicles": vehicles, "allow_real_control": True}
    )


class SyntheticCloud:
    def __init__(self, config):
        self.config = config
        self.requests = []
        self.auth_states = {}
        self.login_count = 0
        self.refresh_count = 0
        self.scope_count = {}
        self.sends = []
        self.climate = {}
        self.records = {
            a.account_id: [
                {
                    "vin": v.vin.get_secret_value(),
                    "vehicleType": "owned",
                    "vehicleState": "Active",
                    "modelId": v.model_id,
                    "vehicleRoleId": 1,
                    "isReceiver": False,
                }
                for v in config.vehicles
                if v.account_id == a.account_id
            ]
            for a in config.accounts
        }
        self.challenge = {
            "option": "bcrypt$2a$04$",
            "snonce": "fixture:" + "ab" * 16,
            "seeded": "01" * 16,
        }
        self.login_redirect = None
        self.scope_error_once = False
        self.refresh_error = False
        self.read_401 = 0
        self.send_status = 200
        self.send_timeout = False
        self.send_missing_receipt = False
        self.result = {"pushState": 5, "resultCode": 0}
        self.invalid_path = None
        self.stale = False
        self.scope_subset = False

    async def handle(self, request):
        self.requests.append(request)
        await asyncio.sleep(0)
        path, host = request.url.path, request.url.host
        form = (
            {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"
            )
            else {}
        )
        if host == "account.lixiang.com":
            assert path == "/login"
            return httpx.Response(200, text="synthetic login page")
        if host == "id.lixiang.com":
            assert "authorization" not in request.headers
            if path == "/api/auth" and form["response_type"] == "code":
                self.login_count += 1
                self.auth_states[form["device_id"]] = form
                return httpx.Response(
                    300,
                    headers={"set-cookie": "auth_session=synthetic-auth; Path=/; Secure; HttpOnly"},
                )
            device = request.headers["idaas-data"].split("device_id=")[1].split(";")[0]
            if path == "/api/devices":
                assert json.loads(request.content)["device_id"] == device
                return httpx.Response(200, json={})
            if path == "/api/idps":
                assert "auth_session=synthetic-auth" in request.headers["cookie"]
                return httpx.Response(200, json={"t_login_use": self.challenge})
            if path == "/api/login":
                proof = json.loads(request.content)["proof"]
                assert set(proof) == {"cnonce", "snonce", "proof"}
                location = (
                    self.login_redirect
                    or self.config.profile.redirect_uri
                    + "?"
                    + urlencode(
                        {
                            "code": "synthetic-code",
                            "state": self.auth_states[device]["state"],
                        }
                    )
                )
                return httpx.Response(
                    302,
                    headers={
                        "location": location,
                        "set-cookie": f"sso_token=synthetic-sso-{device}; Path=/; Secure; HttpOnly",
                    },
                )
            if path == "/api/token":
                if form["grant_type"] == "refresh_token":
                    self.refresh_count += 1
                    assert form["refresh_token"].startswith("synthetic-refresh")
                    if self.refresh_error:
                        return httpx.Response(401, json={})
                else:
                    challenge = (
                        base64.urlsafe_b64encode(
                            hashlib.sha256(form["code_verifier"].encode()).digest()
                        )
                        .rstrip(b"=")
                        .decode()
                    )
                    assert self.auth_states[device]["code_challenge"] == challenge
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"synthetic-main-{device}",
                        "refresh_token": f"synthetic-refresh-{self.refresh_count}",
                        "expires_in": 3600,
                    },
                )
            if path == "/api/auth":
                assert form["device_id"] == device
                assert f"sso_token=synthetic-sso-{device}" in request.headers["cookie"]
                key = (device, form["audience"], form["scope"])
                self.scope_count[key] = self.scope_count.get(key, 0) + 1
                fragment = {
                    "access_token": f"synthetic-scoped-{device}-{len(self.requests)}",
                    "expires_in": 900,
                    "scope": form["scope"],
                }
                if self.scope_error_once:
                    self.scope_error_once = False
                    fragment = {"error": "login_required"}
                if self.scope_subset:
                    fragment["scope"] = "wrong-scope"
                return httpx.Response(
                    302,
                    headers={
                        "location": self.config.profile.redirect_uri + "#" + urlencode(fragment)
                    },
                )
        assert host == "api-app.lixiang.com"
        # No SSO cookies or main/gateway token crosses to the vehicle API.
        assert "cookie" not in request.headers
        assert request.headers["authorization"].startswith("Bearer synthetic-scoped-")
        device = request.headers["x-chj-deviceid"]
        account = next(a for a in self.config.accounts if a.device_id.get_secret_value() == device)
        assert request.headers["x-chj-key"] == account.key_id.get_secret_value()
        if path.startswith("/saos-vehicle-api/"):
            return httpx.Response(200, json={"data": self.records[account.account_id]})
        if path == VSS:
            body = json.loads(request.content)
            assert body["vin"] == request.headers["x-chj-vin"]
            if self.read_401:
                self.read_401 -= 1
                return httpx.Response(401, json={})
            if self.invalid_path in body["paths"]:
                return httpx.Response(400, text="invalid_path|desc:synthetic-path")
            timestamp = "2020-01-01T00:00:00Z" if self.stale else now().isoformat()
            enabled, temperature = self.climate.get(body["vin"], (False, 22))
            items = []
            for item in body["paths"]:
                if item == LOCATION_PATH:
                    value = json.dumps({"v": True, "lat": 0, "lon": 0})
                elif item in BOOLS:
                    value = int(enabled) if "FOffStatus" in item else 0
                else:
                    assert item in NUMBERS
                    value = temperature if "SetTemp" in item else 40
                items.append({"path": item, "dp": {"value": value, "tsFormat": timestamp}})
            return httpx.Response(200, json={"items": items})
        if path == SEND:
            body = json.loads(request.content)
            self.sends.append(body)
            assert body["vin"] == request.headers["x-chj-vin"]
            self.climate[body["vin"]] = (
                body["cmdData"]["acCtrlValue"] == "ON",
                body["cmdData"].get("acCtrlTemp", 22),
            )
            if self.send_timeout:
                raise httpx.ReadTimeout("synthetic-private-response-must-not-escape")
            return httpx.Response(
                self.send_status,
                json={}
                if self.send_missing_receipt
                else {
                    "data": {"requestId": f"synthetic-receipt-{len(self.sends)}"},
                    "resultCode": 0,
                },
            )
        if path.startswith(RESULT):
            return httpx.Response(200, json=self.result)
        raise AssertionError("Unexpected protocol route")

    def transport(self):
        return httpx.MockTransport(self.handle)


@pytest.fixture
def cloud(config):
    return SyntheticCloud(config)
