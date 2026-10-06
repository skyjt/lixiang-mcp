import asyncio

import pytest
from pydantic import ValidationError

from lixiang_mcp.models import ClimateCommand, Phase, Principal, ServiceError
from lixiang_mcp.store import OperationStore


def command(vehicle="demo-l6", key="request-0001", **changes):
    return ClimateCommand(
        vehicle_id=vehicle, enabled=True, temperature_c=23, idempotency_key=key, **changes
    )


async def settled(service, actor, operation):
    for _ in range(150):
        op = await service.operation(actor, operation.operation_id)
        if op.phase in (Phase.VEHICLE_CONFIRMED, Phase.FAILED, Phase.UNKNOWN):
            return op
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not settle")


async def test_concurrent_duplicates_submit_once(service, actor):
    ops = await asyncio.gather(*(service.submit(actor, command()) for _ in range(20)))
    assert len({o.operation_id for o in ops}) == 1
    op = await settled(service, actor, ops[0])
    assert op.phase == Phase.VEHICLE_CONFIRMED
    assert [e.phase for e in op.events] == [
        Phase.SUBMITTED,
        Phase.RUNNING,
        Phase.CLOUD_COMPLETED,
        Phase.VEHICLE_CONFIRMED,
    ]
    assert service.backend.submit_count == 1
    assert (await service.submit(actor, command())).operation_id == op.operation_id
    with pytest.raises(ServiceError, match="idempotency_conflict"):
        await service.submit(actor, command(vehicle="demo-l7"))


async def test_default_disabled_and_scopes(service, actor):
    service.enable_control = False
    with pytest.raises(ServiceError, match="control_disabled"):
        await service.submit(actor, command())
    assert not (await service.capabilities(actor, "demo-l6")).climate
    service.enable_control = True
    readonly = actor.model_copy(update={"scopes": frozenset({"vehicle:read"})})
    with pytest.raises(ServiceError, match="permission_denied"):
        await service.submit(readonly, command())
    with pytest.raises(ServiceError, match="permission_denied"):
        await service.location(readonly, "demo-l6")
    assert "latitude" not in (await service.state(readonly, "demo-l6")).model_dump_json()
    assert (await service.location(actor, "demo-l6")).latitude == 0
    assert service.backend.submit_count == 0


async def test_account_membership_and_grant_both_required(service, actor):
    foreign = Principal(
        subject="other-user",
        account_id="other-account",
        vehicle_ids={"other-l6", "demo-l6"},
        scopes=actor.scopes,
    )
    assert [v.vehicle_id for v in await service.vehicles(foreign)] == ["other-l6"]
    for p, vehicle in [(foreign, "demo-l6"), (actor, "other-l6")]:
        with pytest.raises(ServiceError, match="vehicle_not_found"):
            await service.submit(p, command(vehicle=vehicle))
        with pytest.raises(ServiceError, match="vehicle_not_found"):
            await service.state(p, vehicle)
    operation = await service.submit(actor, command())
    with pytest.raises(ServiceError, match="operation_not_found"):
        await service.operation(foreign, operation.operation_id)
    narrow = actor.model_copy(update={"vehicle_ids": frozenset({"demo-l7"})})
    with pytest.raises(ServiceError, match="vehicle_not_found"):
        await service.operation(narrow, operation.operation_id)
    await settled(service, actor, operation)
    assert (await service.state(actor, "demo-l7")).signals["climate_enabled"].value is False


@pytest.mark.parametrize(
    "fault,phase",
    [
        ("timeout", Phase.UNKNOWN),
        ("reject", Phase.FAILED),
        ("unconfirmed", Phase.UNKNOWN),
        ("stale", Phase.UNKNOWN),
    ],
)
async def test_faults_never_resend(service, actor, fault, phase):
    service.backend.fault = fault
    operation = await service.submit(actor, command())
    final = await settled(service, actor, operation)
    assert final.phase == phase
    duplicate = await service.submit(actor, command())
    assert duplicate.operation_id == operation.operation_id
    assert service.backend.submit_count == 1
    if phase == Phase.UNKNOWN:
        with pytest.raises(ServiceError, match="vehicle_has_unknown_operation"):
            await service.submit(actor, command(key="another-key"))
    if fault in ("unconfirmed", "stale"):
        assert Phase.CLOUD_COMPLETED in [e.phase for e in final.events]


async def test_unknown_model_and_old_state(service, actor):
    caps = await service.capabilities(actor, "demo-unknown")
    assert not caps.model_known and not caps.climate and not caps.pet_mode
    assert not caps.charging_control
    with pytest.raises(ServiceError, match="unsupported_capability"):
        await service.submit(actor, command(vehicle="demo-unknown"))
    service.backend.fault = "stale"
    state = await service.state(actor, "demo-l6")
    assert state.stale and all(s.stale and s.sampled_at for s in state.signals.values())


async def test_serial_per_vehicle_parallel_across_vehicles(service, actor):
    backend = service.backend
    original_send, original_result = backend.submit_climate, backend.result
    active = set()
    peak = 0
    receipts = {}

    async def send(cmd):
        nonlocal peak
        assert cmd.vehicle_id not in active
        active.add(cmd.vehicle_id)
        peak = max(peak, len(active))
        receipt = await original_send(cmd)
        receipts[receipt] = cmd.vehicle_id
        return receipt

    async def result(receipt):
        response = await original_result(receipt)
        active.remove(receipts[receipt])
        return response

    backend.submit_climate, backend.result = send, result
    operations = await asyncio.gather(
        service.submit(actor, command(key="serial-0001")),
        service.submit(actor, command(key="serial-0002")),
        service.submit(actor, command(vehicle="demo-l7", key="parallel-001")),
    )
    final = await asyncio.gather(*(settled(service, actor, op) for op in operations))
    assert all(o.phase == Phase.VEHICLE_CONFIRMED for o in final)
    assert peak == 2


async def test_queued_command_blocked_after_unknown(service, actor):
    service.backend.fault = "unconfirmed"
    ops = [await service.submit(actor, command(key=f"queued-000{i}")) for i in range(2)]
    first, second = await asyncio.gather(*(settled(service, actor, op) for op in ops))
    assert first.phase == Phase.UNKNOWN
    assert second.phase == Phase.FAILED
    assert service.backend.submit_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature_c": 15},
        {"temperature_c": 31},
        {"temperature_c": 22.5},
        {"temperature_c": "23"},
        {"temperature_c": True},
        {"temperature_c": None},
        {"enabled": "true"},
        {"enabled": False},
        {"confirmed": True},
        {"vehicle_id": ""},
        {"idempotency_key": "tiny"},
    ],
)
def test_invalid_climate_arguments(changes):
    data = command().model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        ClimateCommand.model_validate(data)


def test_persistent_idempotency_recovery_and_single_worker(tmp_path, actor):
    path = tmp_path / "ops.sqlite"
    store = OperationStore(path)
    op = store.create(actor.subject, command())
    with pytest.raises(RuntimeError, match="already_in_use"):
        OperationStore(path)
    store.transition(op.operation_id, Phase.RUNNING)
    store.close()
    reopened = OperationStore(path)
    recovered = reopened.existing(actor.subject, command())
    assert recovered.operation_id == op.operation_id and recovered.phase == Phase.UNKNOWN
    assert reopened.unresolved("demo-l6")
    reopened.close()


async def test_ownership_revoked_while_waiting(service, actor):
    lock = asyncio.Lock()
    await lock.acquire()
    service._locks["demo-l6"] = lock
    op = await service.submit(actor, command())
    service.backend.catalog["demo-account"] = []
    lock.release()
    await asyncio.gather(*service._tasks)
    assert service.store.get(op.operation_id).phase == Phase.FAILED
    assert service.backend.submit_count == 0


async def test_control_disabled_or_capability_changed_while_queued(service, actor):
    lock = asyncio.Lock()
    await lock.acquire()
    service._locks["demo-l6"] = lock
    op = await service.submit(actor, command())
    service.enable_control = False
    lock.release()
    await asyncio.gather(*service._tasks)
    assert service.store.get(op.operation_id).phase == Phase.FAILED
    assert service.backend.submit_count == 0


async def test_timeout_may_have_changed_vehicle_but_is_still_unknown(service, actor):
    service.backend.fault = "timeout"
    final = await settled(service, actor, await service.submit(actor, command()))
    assert final.phase == Phase.UNKNOWN
    assert (await service.state(actor, "demo-l6")).signals["climate_enabled"].value is True
    assert service.backend.submit_count == 1
