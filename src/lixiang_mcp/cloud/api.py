"""Narrow LiApiClient protocol adaptation, ha-lixiang v1.3.2.

Copyright (c) 2026 ha-lixiang contributors. MIT: licenses/ha-lixiang-MIT.txt.
No public arbitrary request/command method. No retries after sending a vehicle command.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..models import ClimateCommand, ServiceError
from ..protocol import climate_payload
from .auth import AuthSession
from .config import Profile
from .crypto import Signer
from .transport import API, CloudHTTP, ProtocolError, object_body

VEHICLES = (
    "/saos-vehicle-api/v2-0/vehicles/basics"
    "?types=owned,transferring,authorized,inviting&roleIds=1,10,11,13,15&vehicleInfo=true"
)
VSS = "/ssp-cloud-vss-service/mobile/vss/get-batch"
SEND = "/ssp-vehicle-control-service/ssp-vehicle-control/cmd/send"
RESULT = "/ssp-vehicle-control-service/ssp-vehicle-control/cmd-result/"
MESH_SCOPE = "veh-ctrl:cmd-send veh-ctrl:cmd-result-get"


class VehicleAPI:
    def __init__(
        self,
        profile: Profile,
        auth: AuthSession,
        signer: Signer,
        http: CloudHTTP,
        *,
        allow_control: bool = False,
    ) -> None:
        self.profile, self.auth, self.signer, self.http = profile, auth, signer, http
        self.allow_control = allow_control

    async def _call(
        self,
        method: str,
        path: str,
        audience: str,
        scope: str,
        *,
        vin: str = "",
        payload: dict[str, Any] | None = None,
        write: bool = False,
        prepared_bearer: str | None = None,
    ) -> dict[str, Any]:
        # Called only by explicit endpoint methods below. Paths cannot be supplied by MCP.
        body = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
            if payload is not None
            else b""
        )
        bearer = prepared_bearer or await self.auth.scope(audience, scope)
        for attempt in range(2):
            headers = self.signer.headers(method, body, bearer=bearer, vin=vin)
            response = await self.http.request(
                method, API + path, headers=headers, content=body or None
            )
            if response.status_code == 401 and not write and attempt == 0:
                await self.auth.invalidate(audience, scope, bearer)
                bearer = prepared_bearer or await self.auth.scope(audience, scope)
                continue
            if response.status_code == 400 and path == VSS:
                if re.search(r"\binvalid_path\b", response.text):
                    raise ProtocolError("invalid_vss_path")
            if response.status_code != 200:
                raise ProtocolError("command_outcome_unknown" if write else "upstream_http_error")
            if path == VEHICLES:
                try:
                    data = response.json()
                except ValueError:
                    raise ProtocolError("invalid_upstream_json") from None
                if isinstance(data, list):
                    return {"data": data}
            return object_body(response)
        raise ProtocolError("upstream_auth_failed")

    async def vehicles(self) -> list[dict[str, Any]]:
        response = await self._call("GET", VEHICLES, self.profile.vehicles_audience, "login")
        data = response.get("data")
        if not isinstance(data, list) or not all(isinstance(v, dict) for v in data):
            raise ProtocolError("invalid_vehicle_list")
        return data

    async def vss(self, vin: str, paths: list[str]) -> dict[str, Any]:
        if not paths or len(paths) > 50:
            raise ProtocolError("invalid_vss_batch")
        items: list[Any] = []

        async def fetch(batch: list[str]) -> None:
            try:
                response = await self._call(
                    "POST",
                    VSS,
                    self.profile.vss_audience,
                    "vss:get-batch",
                    vin=vin,
                    payload={"vin": vin, "paths": batch},
                )
                part = response.get("items")
                if not isinstance(part, list):
                    raise ProtocolError("invalid_vss_response")
                items.extend(part)
            except ProtocolError as exc:
                if exc.code != "invalid_vss_path":
                    raise
                if len(batch) == 1:
                    return
                midpoint = len(batch) // 2
                await fetch(batch[:midpoint])
                await fetch(batch[midpoint:])

        await fetch(paths)
        return {"items": items}

    async def submit_climate(self, vin: str, command: ClimateCommand) -> str:
        if not self.allow_control:
            raise ServiceError("real_control_disabled")
        # Acquire both tokens before the one permitted command POST.
        mesh, vat = await self.auth.bundle(
            [
                (self.profile.mesh_audience, MESH_SCOPE, 780),
                (self.profile.vat_audience, f"remoteVehACSmartControl:{vin}", 19 * 3600),
            ]
        )
        response = await self._call(
            "POST",
            SEND,
            self.profile.mesh_audience,
            MESH_SCOPE,
            vin=vin,
            write=True,
            prepared_bearer=mesh,
            payload={
                "vin": vin,
                "cmdKey": "remoteVehACSmartControl",
                "cmdData": climate_payload(command),
                "domain": "xcu",
                "jobExpire": 900,
                "expire": 900,
                "expireAt": int(time.time() * 1000) + 900000,
                "token": vat,
            },
        )
        code = response.get("resultCode")
        nested = response.get("data")
        receipt = response.get("requestId") or (
            nested.get("requestId") if isinstance(nested, dict) else None
        )
        if code is not None and (type(code) not in (int, str) or code not in (0, "0")):
            if receipt or type(code) not in (int, str):
                raise ProtocolError("command_response_conflict")
            raise ServiceError("cloud_rejected")
        if not isinstance(receipt, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", receipt):
            raise ProtocolError("command_receipt_unknown")
        return receipt

    async def result(self, vin: str, receipt: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", receipt):
            raise ProtocolError("invalid_receipt")
        return await self._call(
            "GET", RESULT + receipt, self.profile.mesh_audience, MESH_SCOPE, vin=vin
        )
