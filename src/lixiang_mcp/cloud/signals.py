"""Limited VSS projection from ha-lixiang signals.py/rendering.py v1.3.2.

Copyright (c) 2026 ha-lixiang contributors. MIT: licenses/ha-lixiang-MIT.txt.
Unknown encodings are unknown, never inferred by Python truthiness.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..models import Location, Sample, VehicleState, now
from ..protocol import parse_vss

# name, unit, minimum, maximum; only these paths can reach ordinary state calls.
NUMBERS: dict[str, tuple[str, str | None, float, float]] = {
    "Vehicle.Powertrain.Battery.ResidueBattery": ("battery_percent", "%", 0, 100),
    "Vehicle.Cabin.CLTC.PureElecEnduranceMileInd": ("electric_range_km", "km", 0, 10000),
    "Vehicle.Cabin.CLTC.FuelEnduranceMileInd": ("fuel_range_km", "km", 0, 10000),
    "Vehicle.Cabin.AC.SetTemp": ("climate_target_c", "°C", 16, 30),
    "Vehicle.Powertrain.Battery.CLTCChargePower": ("charge_power_kw", "kW", 0, 1000),
    "Vehicle.Powertrain.Battery.ChargeSurplusTime": ("charge_remaining_minutes", "min", 0, 100000),
    "Vehicle.Powertrain.Battery.ChargeStatus": ("charge_status_code", None, 0, 1000),
}
for suffix, name in (
    ("FL", "front_left"),
    ("FR", "front_right"),
    ("RL", "rear_left"),
    ("RR", "rear_right"),
):
    NUMBERS[f"Vehicle.Chassis.Tire.{suffix}TirePressure"] = (f"tire_{name}_kpa", "kPa", 0, 1000)
for suffix, name in (
    ("Main", "front_left"),
    ("Copilot", "front_right"),
    ("BackLeft", "rear_left"),
    ("BackRight", "rear_right"),
):
    NUMBERS[f"Vehicle.Body.WindowPosition.{suffix}Window"] = (f"window_{name}_percent", "%", 0, 100)

BOOLS: dict[str, tuple[str, frozenset[int]]] = {
    "Vehicle.Cabin.AC.FOffStatus": ("climate_enabled", frozenset({1})),
    "Vehicle.Powertrain.Battery.ACChgrActualConnSts": (
        "charge_ac_plug_connected",
        frozenset({1, 2}),
    ),
    "Vehicle.Powertrain.Battery.DCChrgngGunActuSts": (
        "charge_dc_plug_connected",
        frozenset({1, 2}),
    ),
}
for suffix, name in (
    ("Main", "front_left"),
    ("Copilot", "front_right"),
    ("BackLeft", "rear_left"),
    ("BackRight", "rear_right"),
):
    BOOLS[f"Vehicle.Body.DoorSwitchStatus.{suffix}Door"] = (f"door_{name}_open", frozenset({1}))
for channel in ("5G", "hu-f", "xcu"):
    BOOLS[f"Vehicle.ConnectManager.ConnectStatus.{channel}"] = (
        f"connected_{channel}",
        frozenset({1}),
    )

PATHS = {path: (value[0], value[1]) for path, value in NUMBERS.items()} | {
    path: (value[0], None) for path, value in BOOLS.items()
}
LOCATION_PATH = "Vehicle.Location.CurrentLocationInfo"


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def converted(source: Sample, value: float | bool | None, unit: str | None = None) -> Sample:
    return source.model_copy(
        update={"value": value, "stale": source.stale or value is None, "unit": unit}
    )


def combine(values: list[Sample], *, sum_values: bool = False) -> Sample:
    timestamps = [s.sampled_at for s in values if s.sampled_at is not None]
    complete = len(timestamps) == len(values) and all(s.value is not None for s in values)
    value: float | bool | None = None
    if complete:
        if sum_values:
            numbers = [float(s.value) for s in values if isinstance(s.value, (int, float))]
            value = sum(numbers, 0.0) if len(numbers) == len(values) else None
        else:
            value = any(s.value is True for s in values)
    elif not sum_values and any(s.value is True and not s.stale for s in values):
        value = True  # A fresh positive is enough, missing channels cannot prove offline.
    return Sample(
        value=value,
        unit="km" if sum_values else None,
        sampled_at=min(timestamps) if timestamps else None,
        stale=not complete or any(s.stale for s in values),
    )


def state_from_vss(
    vehicle_id: str, response: dict[str, Any], ttl: float, *, simulated: bool
) -> VehicleState:
    observed = now()
    signals = parse_vss(response, PATHS, observed, ttl)
    for _path, (name, unit, lower, upper) in NUMBERS.items():
        value = numeric(signals[name].value)
        signals[name] = converted(
            signals[name], value if value is not None and lower <= value <= upper else None, unit
        )
    for _path, (name, positive) in BOOLS.items():
        raw = signals[name].value
        if isinstance(raw, bool):
            value_bool: bool | None = raw
        elif isinstance(raw, str) and raw.lower() in ("true", "false"):
            value_bool = raw.lower() == "true"
        else:
            number = numeric(raw)
            value_bool = number in positive if number in positive | {0} else None
        signals[name] = converted(signals[name], value_bool)
    for name in list(signals):
        if name.startswith("window_") and name.endswith("_percent"):
            position = numeric(signals[name].value)
            signals[name.replace("_percent", "_open")] = converted(
                signals[name], position > 0 if position is not None else None
            )
    signals["connected"] = combine([signals[f"connected_{c}"] for c in ("5G", "hu-f", "xcu")])
    signals["charge_plug_connected"] = combine(
        [signals["charge_ac_plug_connected"], signals["charge_dc_plug_connected"]]
    )
    signals["total_range_km"] = combine(
        [signals["electric_range_km"], signals["fuel_range_km"]], sum_values=True
    )
    raw_status = signals["charge_status_code"].value
    # Preserve raw code alongside the conservative mapping; no fake normalized state labels.
    charging = (
        {0: False, 3: True, 10: False, 15: False, 30: True, 70: True, 130: False}.get(
            int(raw_status)
        )
        if isinstance(raw_status, (int, float)) and float(raw_status).is_integer()
        else None
    )
    signals["charging"] = converted(signals["charge_status_code"], charging)
    return VehicleState(
        vehicle_id=vehicle_id,
        observed_at=observed,
        signals=signals,
        stale=any(s.stale for s in signals.values()),
        simulated=simulated,
    )


def location_from_vss(
    vehicle_id: str, response: dict[str, Any], ttl: float, *, simulated: bool
) -> Location:
    observed = now()
    source = parse_vss(response, {LOCATION_PATH: ("location", None)}, observed, ttl)["location"]
    lat = lon = None
    try:
        data = json.loads(source.value) if isinstance(source.value, str) else None
        if isinstance(data, dict) and data.get("v") is True:
            lat, lon = numeric(data.get("lat")), numeric(data.get("lon"))
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                lat = lon = None
    except ValueError:
        pass
    return Location(
        vehicle_id=vehicle_id,
        latitude=lat,
        longitude=lon,
        sampled_at=source.sampled_at,
        stale=source.stale or lat is None or lon is None,
        simulated=simulated,
    )
