import asyncio
import io
import logging
import time
from datetime import timedelta

import pytest

from lixiang_mcp.models import ClimateCommand, now
from lixiang_mcp.protocol import climate_payload, cloud_result, parse_vss, sample
from lixiang_mcp.safe_logging import SafeLogFilter
from lixiang_mcp.session import (
    SessionManager,
    UnconfiguredLogin,
    UnconfiguredSigner,
    VehicleSession,
)


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"pushState": 5, "resultCode": 0}, "completed"),
        ({"pushState": 5, "resultCode": "-15"}, "completed"),
        ({"pushState": 5, "resultCode": "-8"}, "completed"),
        ({"pushState": 5, "resultCode": True}, "failed"),
        ({"pushState": 5}, "failed"),
        ({"pushState": 7}, "failed"),
        ({"pushState": 3}, "pending"),
        ({}, "pending"),
    ],
)
def test_cloud_result(data, expected):
    assert cloud_result(data) == expected


def test_protocol_payload_and_timestamp():
    cmd = ClimateCommand(
        vehicle_id="demo-l6", enabled=True, temperature_c=23, idempotency_key="test-key-001"
    )
    assert climate_payload(cmd) == {
        "acCtrlType": "frtACSw",
        "acCtrlValue": "ON",
        "acCountdownTimer": "15",
        "acCtrlTemp": 23,
    }
    off = ClimateCommand(vehicle_id="demo-l6", enabled=False, idempotency_key="test-key-002")
    assert "acCtrlTemp" not in climate_payload(off)
    observed = now()
    path = "Vehicle.Powertrain.Battery.ResidueBattery"
    parsed = parse_vss(
        {
            "items": [
                {"path": path, "dp": {"value": 72, "tsFormat": observed.isoformat()}},
                {"path": "Vehicle.Location.CurrentLocationInfo", "dp": {"value": "ignored"}},
            ]
        },
        {path: ("battery_percent", "%"), "missing": ("missing", None)},
        observed,
        120,
    )
    assert set(parsed) == {"battery_percent", "missing"}
    assert not parsed["battery_percent"].stale and parsed["missing"].stale
    assert sample(1, "2026-01-01T12:00:00", observed, 120).stale  # no timezone
    assert sample(1, (observed + timedelta(minutes=5)).isoformat(), observed, 120).stale
    assert sample(1, "invalid", observed, 120).stale


async def test_account_renewal_lock_and_isolation():
    calls = []

    class Provider:
        async def renew(self, account_id):
            calls.append(account_id)
            await asyncio.sleep(0.01)
            return VehicleSession("synthetic-session", time.monotonic() + 300)

    manager = SessionManager(Provider())
    results = await asyncio.gather(*(manager.get("a") for _ in range(20)), manager.get("b"))
    assert sorted(calls) == ["a", "b"]
    assert all(result.expires_at_monotonic > time.monotonic() for result in results)
    assert "synthetic-session" not in repr(results[0])
    manager._sessions["a"] = VehicleSession("expired", 0)
    await asyncio.gather(*(manager.get("a") for _ in range(20)))
    assert calls.count("a") == 2
    with pytest.raises(RuntimeError, match="not_implemented"):
        await UnconfiguredLogin().renew("a")
    with pytest.raises(RuntimeError, match="not_implemented"):
        await UnconfiguredSigner().sign(method="GET", path="/", body=b"")


def test_logs_omit_sensitive_free_text_args_and_tracebacks():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(SafeLogFilter())
    logger = logging.getLogger("lixiang_mcp.test_privacy")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for text in [
            "Bearer synthetic-sensitive-value",
            "phone=synthetic-phone",
            "VIN=synthetic-vin",
            "location=synthetic-coordinate",
            "device=synthetic-id",
        ]:
            logger.info("upstream: %s", text)
        try:
            raise RuntimeError("private-response")
        except RuntimeError:
            logger.exception("sensitive-error")
        logger.info("operation_confirmed")
    finally:
        logger.removeHandler(handler)
    value = output.getvalue()
    assert "operation_confirmed" in value
    for marker in ["synthetic", "private-response", "sensitive-error", "Traceback"]:
        assert marker not in value
