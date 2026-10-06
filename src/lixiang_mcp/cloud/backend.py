from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from ..models import (
    Capabilities,
    ClimateCommand,
    CommandRejected,
    Location,
    ServiceError,
    Vehicle,
    VehicleState,
)
from .api import VehicleAPI
from .auth import AuthSession
from .config import CloudConfig, VehicleBinding
from .crypto import Signer
from .signals import LOCATION_PATH, PATHS, location_from_vss, state_from_vss
from .transport import ACCOUNT, API, ID, CloudHTTP


def usable_relation(record: dict[str, Any], *, write: bool = False) -> bool:
    """Narrower than upstream vehicle_role.py: unknown/trial/inviting/transferring fail closed."""
    if record.get("vehicleState") in (None, "", "Registered", "Transferred", "ReverseActivating"):
        return False
    if any(
        record.get(k) in (True, 1, "1", "true")
        for k in ("isReceiver", "receiver", "isTransferReceiver")
    ):
        return False
    if record.get("vehicleType") == "owned":
        # Upstream could not recover isReceiver(); absence is insufficient proof for a write.
        return not write or any(
            record.get(k) is False for k in ("isReceiver", "receiver", "isTransferReceiver")
        )
    # Family scope compatibility for writes is not verified, so only read here.
    return (
        not write
        and record.get("vehicleType") == "authorized"
        and str(record.get("vehicleRoleId")) == "15"
    )


class CloudBackend:
    def __init__(
        self,
        config: CloudConfig,
        *,
        ttl: float = 120,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self.config, self.ttl = config, ttl
        # Mock transport injection is a Python test seam, never a JSON/MCP configuration choice.
        self.simulated = transport_factory is not None
        self.bindings = {v.vehicle_id: v for v in config.vehicles}
        self._clients: list[CloudHTTP] = []
        self._apis: dict[str, VehicleAPI] = {}
        self._receipts: dict[str, tuple[VehicleBinding, str]] = {}
        identity = [(v.account_id, v.vehicle_id, v.vin.get_secret_value()) for v in config.vehicles]
        namespace = hashlib.sha256(json.dumps(sorted(identity)).encode()).hexdigest()
        self.storage_namespace = f"{'contract' if self.simulated else 'lixiang'}:{namespace}"
        for account in config.accounts:
            login_http = CloudHTTP(
                frozenset({ID, ACCOUNT}),
                transport=transport_factory() if transport_factory else None,
            )
            api_http = CloudHTTP(
                frozenset({API}), transport=transport_factory() if transport_factory else None
            )
            self._clients.extend((login_http, api_http))
            auth = AuthSession(config.profile, account, login_http)
            self._apis[account.account_id] = VehicleAPI(
                config.profile,
                auth,
                Signer(config.profile, account),
                api_http,
                allow_control=config.allow_real_control,
            )

    def _binding(self, vehicle_id: str) -> VehicleBinding:
        binding = self.bindings.get(vehicle_id)
        if binding is None:
            raise ServiceError("vehicle_not_found")
        return binding

    async def _record(self, binding: VehicleBinding, *, write: bool = False) -> dict[str, Any]:
        records = await self._apis[binding.account_id].vehicles()
        matching = [r for r in records if r.get("vin") == binding.vin.get_secret_value()]
        if len(matching) != 1 or not usable_relation(matching[0], write=write):
            raise ServiceError("vehicle_not_found")
        return matching[0]

    async def vehicles(self, account_id: str) -> list[Vehicle]:
        api = self._apis.get(account_id)
        if api is None:
            return []
        records = await api.vehicles()
        vehicles = []
        for binding in self.bindings.values():
            if binding.account_id != account_id:
                continue
            rows = [r for r in records if r.get("vin") == binding.vin.get_secret_value()]
            if len(rows) == 1 and usable_relation(rows[0]):
                vehicles.append(
                    Vehicle(
                        vehicle_id=binding.vehicle_id,
                        model=binding.model_label,
                        label=binding.label,
                        simulated=self.simulated,
                    )
                )
        return vehicles

    async def capabilities(self, vehicle_id: str) -> Capabilities:
        binding = self._binding(vehicle_id)
        record = await self._record(binding)
        known = str(record.get("modelId")) == binding.model_id
        can_control = (
            known
            and binding.climate_supported
            and self.config.allow_real_control
            and usable_relation(record, write=True)
        )
        return Capabilities(
            model_known=known,
            climate=can_control,
            location=known and binding.location_supported,
            temperature_min_c=16 if can_control else None,
            temperature_max_c=30 if can_control else None,
            temperature_step_c=1 if can_control else None,
            simulated=self.simulated,
        )

    async def state(self, vehicle_id: str) -> VehicleState:
        binding = self._binding(vehicle_id)
        await self._record(binding)
        response = await self._apis[binding.account_id].vss(
            binding.vin.get_secret_value(), list(PATHS)
        )
        return state_from_vss(vehicle_id, response, self.ttl, simulated=self.simulated)

    async def location(self, vehicle_id: str) -> Location:
        binding = self._binding(vehicle_id)
        if not (await self.capabilities(vehicle_id)).location:
            raise ServiceError("unsupported_capability")
        response = await self._apis[binding.account_id].vss(
            binding.vin.get_secret_value(), [LOCATION_PATH]
        )
        return location_from_vss(vehicle_id, response, self.ttl, simulated=self.simulated)

    async def submit_climate(self, command: ClimateCommand) -> str:
        try:
            if not self.config.allow_real_control:
                raise ServiceError("real_control_disabled")
            command = ClimateCommand.model_validate(command.model_dump())
            binding = self._binding(command.vehicle_id)
            record = await self._record(binding, write=True)
            if str(record.get("modelId")) != binding.model_id or not binding.climate_supported:
                raise ServiceError("unsupported_capability")
        except ServiceError as exc:
            # This block is strictly before the one command POST.
            raise CommandRejected(exc.code) from None
        receipt = await self._apis[binding.account_id].submit_climate(
            binding.vin.get_secret_value(), command
        )
        opaque = str(uuid.uuid4())
        self._receipts[opaque] = (binding, receipt)
        return opaque

    async def result(self, receipt: str) -> dict[str, Any]:
        entry = self._receipts.get(receipt)
        if entry is None:
            raise ServiceError("operation_not_found")
        binding, upstream_id = entry
        return await self._apis[binding.account_id].result(
            binding.vin.get_secret_value(), upstream_id
        )

    async def close(self) -> None:
        for client in self._clients:
            await client.close()
