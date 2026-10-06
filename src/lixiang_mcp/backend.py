from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal, Protocol

from .models import Capabilities, ClimateCommand, Location, ServiceError, Vehicle, VehicleState, now
from .protocol import climate_payload, sample

Fault = Literal["none", "timeout", "reject", "unconfirmed", "stale"]


class Backend(Protocol):
    simulated: bool

    async def vehicles(self, account_id: str) -> list[Vehicle]: ...
    async def capabilities(self, vehicle_id: str) -> Capabilities: ...
    async def state(self, vehicle_id: str) -> VehicleState: ...
    async def location(self, vehicle_id: str) -> Location: ...
    async def submit_climate(self, command: ClimateCommand) -> str: ...
    async def result(self, receipt: str) -> dict[str, int]: ...


class MockBackend:
    """Only invented data. Fault injection is local configuration, never an MCP argument."""

    simulated = True

    def __init__(self, *, fault: Fault = "none", ttl: float = 120) -> None:
        self.fault, self.ttl = fault, ttl
        self.submit_count = 0
        self.catalog = {
            "demo-account": [
                Vehicle(vehicle_id="demo-l6", model="SIM-L6", label="模拟 L6"),
                Vehicle(vehicle_id="demo-l7", model="SIM-L7", label="模拟 L7"),
                Vehicle(vehicle_id="demo-unknown", model="UNKNOWN", label="未知车型"),
            ],
            "other-account": [Vehicle(vehicle_id="other-l6", model="SIM-L6", label="隔离车辆")],
        }
        self._climate: dict[str, tuple[bool, int]] = {}
        self._receipts: dict[str, ClimateCommand] = {}

    async def vehicles(self, account_id: str) -> list[Vehicle]:
        return self.catalog.get(account_id, [])

    async def capabilities(self, vehicle_id: str) -> Capabilities:
        known = any(
            v.vehicle_id == vehicle_id and v.model.startswith("SIM-")
            for vehicles in self.catalog.values()
            for v in vehicles
        )
        return Capabilities(
            model_known=known,
            climate=known,
            location=known,
            temperature_min_c=16 if known else None,
            temperature_max_c=30 if known else None,
            temperature_step_c=1 if known else None,
        )

    async def state(self, vehicle_id: str) -> VehicleState:
        observed = now()
        timestamp = observed - timedelta(seconds=600 if self.fault == "stale" else 0)
        on, temperature = self._climate.get(vehicle_id, (False, 22))
        values: dict[str, tuple[float | int | bool | str, str | None]] = {
            "connected": (True, None),
            "battery_percent": (72, "%"),
            "electric_range_km": (148, "km"),
            "total_range_km": (820, "km"),
            "tire_front_left_kpa": (250, "kPa"),
            "tire_front_right_kpa": (251, "kPa"),
            "tire_rear_left_kpa": (252, "kPa"),
            "tire_rear_right_kpa": (251, "kPa"),
            "door_front_left_open": (False, None),
            "door_front_right_open": (False, None),
            "door_rear_left_open": (False, None),
            "door_rear_right_open": (False, None),
            "window_front_left_open": (False, None),
            "window_front_right_open": (False, None),
            "window_rear_left_open": (False, None),
            "window_rear_right_open": (False, None),
            "charging": (False, None),
            "charge_plug_connected": (False, None),
            "charge_power_kw": (0.0, "kW"),
            "charge_remaining_minutes": (0, "min"),
            "climate_enabled": (on, None),
            "climate_target_c": (temperature, "°C"),
        }
        signals = {
            name: sample(value, timestamp.isoformat(), observed, self.ttl, unit)
            for name, (value, unit) in values.items()
        }
        return VehicleState(
            vehicle_id=vehicle_id,
            observed_at=observed,
            signals=signals,
            stale=any(s.stale for s in signals.values()),
        )

    async def location(self, vehicle_id: str) -> Location:
        # (0, 0) is an explicit fictional fixture, not a recorded vehicle location.
        timestamp = now() - timedelta(seconds=600 if self.fault == "stale" else 0)
        return Location(
            vehicle_id=vehicle_id,
            latitude=0,
            longitude=0,
            sampled_at=timestamp,
            stale=self.fault == "stale",
        )

    async def submit_climate(self, command: ClimateCommand) -> str:
        climate_payload(command)  # Exercise the narrow protocol encoder without networking.
        self.submit_count += 1
        if self.fault == "reject":
            raise ServiceError("cloud_rejected")
        if self.fault == "timeout":
            # Models an accepted request whose response was lost: no automatic retry.
            self._climate[command.vehicle_id] = (command.enabled, command.temperature_c or 22)
            raise TimeoutError
        receipt = f"mock-receipt-{self.submit_count}"
        self._receipts[receipt] = command
        return receipt

    async def result(self, receipt: str) -> dict[str, int]:
        await asyncio.sleep(0.02)
        command = self._receipts[receipt]
        if self.fault != "unconfirmed":
            self._climate[command.vehicle_id] = (command.enabled, command.temperature_c or 22)
        return {"pushState": 5, "resultCode": 0}
