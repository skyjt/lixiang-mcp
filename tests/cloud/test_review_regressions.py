"""Review regressions against synthetic HTTP only, including uncertain command outcomes."""

import asyncio
import gzip
import zlib
from urllib.parse import parse_qs

import httpx
import pytest

from lixiang_mcp.cloud.api import RESULT, SEND
from lixiang_mcp.cloud.backend import CloudBackend
from lixiang_mcp.cloud.transport import API, CloudHTTP, ProtocolError, object_body
from lixiang_mcp.models import ClimateCommand, Phase, Principal, ServiceError
from lixiang_mcp.service import VehicleService
from lixiang_mcp.store import OperationStore


def actor():
    return Principal(
        subject="review-user",
        account_id="account-a",
        vehicle_ids={"car-0-0"},
        scopes={"vehicle:read", "vehicle:climate"},
    )


def command(key="review-operation-1"):
    return ClimateCommand(vehicle_id="car-0-0", enabled=True, temperature_c=23, idempotency_key=key)


def service_for(config, cloud, tmp_path):
    return VehicleService(
        CloudBackend(config, transport_factory=cloud.transport),
        OperationStore(tmp_path / "review.sqlite"),
        enable_control=True,
        command_timeout=2,
    )


async def test_membership_missing_during_confirmation_keeps_unknown_barrier(
    config, cloud, tmp_path, monkeypatch
):
    original_handle = cloud.handle
    original_records = cloud.records["account-a"]

    async def handle(request):
        response = await original_handle(request)
        if request.url.path.startswith(RESULT):
            cloud.records["account-a"] = []
        return response

    monkeypatch.setattr(cloud, "handle", handle)
    service = service_for(config, cloud, tmp_path)
    try:
        operation = await service.submit(actor(), command())
        await asyncio.gather(*service._tasks)
        result = service.store.get(operation.operation_id)
        assert result.phase == Phase.UNKNOWN
        assert Phase.CLOUD_COMPLETED in [e.phase for e in result.events]
        cloud.records["account-a"] = original_records
        assert (await service.operation(actor(), operation.operation_id)).phase == Phase.UNKNOWN
        with pytest.raises(ServiceError, match="vehicle_has_unknown_operation"):
            await service.submit(actor(), command("review-operation-2"))
        assert len(cloud.sends) == 1
    finally:
        await service.close()


@pytest.mark.parametrize("stage", ["submit_climate", "result", "state"])
@pytest.mark.parametrize("code", ["vehicle_not_found", "cloud_rejected"])
async def test_unqualified_service_error_after_dispatch_is_unknown(
    config, cloud, tmp_path, monkeypatch, stage, code
):
    service = service_for(config, cloud, tmp_path)
    original_send = service.backend.submit_climate

    async def failed(*args):
        if stage == "submit_climate":
            await original_send(*args)
        raise ServiceError(code)

    monkeypatch.setattr(service.backend, stage, failed)
    try:
        operation = await service.submit(actor(), command())
        await asyncio.gather(*service._tasks)
        assert service.store.get(operation.operation_id).phase == Phase.UNKNOWN
        with pytest.raises(ServiceError, match="vehicle_has_unknown_operation"):
            await service.submit(actor(), command("review-operation-2"))
        assert len(cloud.sends) == 1
    finally:
        await service.close()


@pytest.mark.parametrize("stage", ["send", "result"])
@pytest.mark.parametrize("code", ["", " ", "not-a-code", False, [], {}])
async def test_invalid_result_codes_never_prove_rejection(
    config, cloud, tmp_path, monkeypatch, stage, code
):
    original_handle = cloud.handle

    async def handle(request):
        response = await original_handle(request)
        if stage == "send" and request.url.path == SEND:
            return httpx.Response(200, json={"resultCode": code})
        return response

    monkeypatch.setattr(cloud, "handle", handle)
    if stage == "result":
        cloud.result = {"pushState": 7, "resultCode": code}
    service = service_for(config, cloud, tmp_path)
    try:
        operation = await service.submit(actor(), command())
        await asyncio.gather(*service._tasks)
        assert service.store.get(operation.operation_id).phase == Phase.UNKNOWN
        with pytest.raises(ServiceError, match="vehicle_has_unknown_operation"):
            await service.submit(actor(), command("review-operation-2"))
        assert len(cloud.sends) == 1
    finally:
        await service.close()


@pytest.mark.parametrize("stage", ["send", "result"])
@pytest.mark.parametrize("code", [2009, "2009"])
async def test_definite_cloud_rejection_is_failed_without_automatic_retry(
    config, cloud, tmp_path, monkeypatch, stage, code
):
    original_handle = cloud.handle

    async def handle(request):
        response = await original_handle(request)
        if stage == "send" and request.url.path == SEND:
            return httpx.Response(200, json={"resultCode": code})
        return response

    monkeypatch.setattr(cloud, "handle", handle)
    if stage == "result":
        cloud.result = {"pushState": 7, "resultCode": code}
    service = service_for(config, cloud, tmp_path)
    try:
        operation = await service.submit(actor(), command())
        await asyncio.gather(*service._tasks)
        result = service.store.get(operation.operation_id)
        assert result.phase == Phase.FAILED and result.error_code == "cloud_rejected"
        assert not service.store.unresolved(command().vehicle_id)
        assert len(cloud.sends) == 1
        assert (await service.submit(actor(), command())).operation_id == operation.operation_id
        assert len(cloud.sends) == 1
    finally:
        await service.close()


@pytest.mark.parametrize("stage", ["login", "scope"])
@pytest.mark.parametrize("status", [200, 302])
async def test_malformed_auth_redirect_blocks_further_login(
    config, cloud, monkeypatch, stage, status
):
    original_handle = cloud.handle

    async def handle(request):
        response = await original_handle(request)
        login = stage == "login" and request.url.path == "/api/login"
        scope = (
            stage == "scope"
            and request.url.path == "/api/auth"
            and parse_qs(request.content.decode()).get("response_type") == ["token"]
        )
        if login or scope:
            return httpx.Response(status, headers={"location": "https://["})
        return response

    monkeypatch.setattr(cloud, "handle", handle)
    backend = CloudBackend(config, transport_factory=cloud.transport)
    try:
        with pytest.raises(ProtocolError, match="invalid_.*redirect") as exc:
            await backend.vehicles("account-a")
        assert "https://[" not in str(exc.value)
        request_count, login_count = len(cloud.requests), cloud.login_count
        with pytest.raises(ProtocolError, match="account_requires_operator_attention"):
            await backend.vehicles("account-a")
        assert len(cloud.requests) == request_count and cloud.login_count == login_count == 1
        assert not cloud.sends
    finally:
        await backend.close()


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_compressed_http_response_decoded_once_and_request_not_retained(encoding, compress):
    payload = b'{"ok":true}'
    compressed = compress(payload)

    async def handle(request):
        return httpx.Response(
            200,
            content=compressed,
            headers={"content-encoding": encoding, "content-length": str(len(compressed))},
        )

    client = CloudHTTP(frozenset({API}), transport=httpx.MockTransport(handle))
    try:
        response = await client.request("GET", API + "/test", headers={"X-Test": "private"})
        assert object_body(response) == {"ok": True}
        assert "content-encoding" not in response.headers
        assert int(response.headers["content-length"]) == len(payload)
        with pytest.raises(RuntimeError, match="request instance"):
            _ = response.request
    finally:
        await client.close()


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_decompressed_response_size_limit_is_preserved(encoding, compress):
    compressed = compress(b"x" * 1_048_577)
    assert len(compressed) < 1_048_576

    async def handle(request):
        return httpx.Response(200, content=compressed, headers={"content-encoding": encoding})

    client = CloudHTTP(frozenset({API}), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ProtocolError, match="upstream_response_too_large"):
            await client.request("GET", API + "/test")
    finally:
        await client.close()
