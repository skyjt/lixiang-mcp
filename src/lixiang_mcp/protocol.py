"""Small, pure protocol adaptations from ha-lixiang v1.3.2.

Copyright (c) 2026 ha-lixiang contributors.
MIT: see licenses/ha-lixiang-MIT.txt and docs/UPSTREAM.md.
No login constants, signing secrets, device IDs, or network calls are included.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from .models import ClimateCommand, Sample


def climate_payload(command: ClimateCommand) -> dict[str, str | int]:
    """Adapted from climate.py: front AC, fixed 15-minute timer, numeric Celsius."""
    data: dict[str, str | int] = {
        "acCtrlType": "frtACSw",
        "acCtrlValue": "ON" if command.enabled else "OFF",
        "acCountdownTimer": "15",
    }
    if command.temperature_c is not None:
        data["acCtrlTemp"] = command.temperature_c
    return data


def cloud_result(data: dict[str, Any]) -> Literal["pending", "completed", "failed", "unknown"]:
    """Contradictory/missing terminal codes cannot prove success or safe rejection."""
    state, code = data.get("pushState"), data.get("resultCode")
    valid_code = type(code) in (int, str)
    success = valid_code and code in (0, "0", -15, "-15", -8, "-8")
    if type(state) is not int:
        return "unknown"
    if state == 5:
        return "completed" if success else "unknown"
    if state == 7:
        return "failed" if valid_code and not success else "unknown"
    return "pending"


def sample(
    value: Any, timestamp: Any, observed: datetime, ttl: float, unit: str | None = None
) -> Sample:
    parsed = None
    if isinstance(timestamp, str):
        try:
            candidate = datetime.fromisoformat(timestamp)
            if candidate.tzinfo is not None:
                parsed = candidate
        except ValueError:
            pass
    if type(value) not in (int, float, str, bool, type(None)):
        value = None
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    age = (observed - parsed).total_seconds() if parsed is not None else None
    return Sample(
        value=value,
        unit=unit,
        sampled_at=parsed,
        stale=value is None or age is None or age < -5 or age > ttl,
    )


def parse_vss(
    data: dict[str, Any],
    allowed_paths: dict[str, tuple[str, str | None]],
    observed: datetime,
    ttl: float,
) -> dict[str, Sample]:
    """Adapted from get_vss_state; explicit allowlist, preserve timezone, retain missing signals."""
    result = {
        name: sample(None, None, observed, ttl, unit) for name, unit in allowed_paths.values()
    }
    for item in data.get("items", []):
        if not isinstance(item, dict) or item.get("path") not in allowed_paths:
            continue
        name, unit = allowed_paths[item["path"]]
        dp = item.get("dp")
        if isinstance(dp, dict):
            result[name] = sample(dp.get("value"), dp.get("tsFormat"), observed, ttl, unit)
    return result
