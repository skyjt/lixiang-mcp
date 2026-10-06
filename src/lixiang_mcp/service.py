from __future__ import annotations

import asyncio
import logging

from .backend import Backend
from .models import (
    Capabilities,
    ClimateCommand,
    CommandRejected,
    Location,
    Operation,
    Phase,
    Principal,
    Scope,
    ServiceError,
    Vehicle,
    VehicleState,
    now,
)
from .protocol import cloud_result
from .store import OperationStore

logger = logging.getLogger(__name__)


class VehicleService:
    def __init__(
        self,
        backend: Backend,
        store: OperationStore,
        *,
        enable_control: bool = False,
        command_timeout: float = 5,
        max_pending: int = 32,
    ) -> None:
        store.bind_backend(backend.storage_namespace)
        self.backend, self.store = backend, store
        self.enable_control, self.command_timeout = enable_control, command_timeout
        self.max_pending = max_pending
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    @staticmethod
    def scope(principal: Principal, scope: Scope) -> None:
        if scope not in principal.scopes:
            raise ServiceError("permission_denied")

    async def authorize(self, principal: Principal, vehicle_id: str, scope: Scope) -> None:
        self.scope(principal, scope)
        # Both administrator grant and current backend account membership must match.
        if vehicle_id not in principal.vehicle_ids or not any(
            v.vehicle_id == vehicle_id for v in await self.backend.vehicles(principal.account_id)
        ):
            raise ServiceError("vehicle_not_found")

    async def vehicles(self, principal: Principal) -> list[Vehicle]:
        self.scope(principal, "vehicle:read")
        return [
            v
            for v in await self.backend.vehicles(principal.account_id)
            if v.vehicle_id in principal.vehicle_ids
        ]

    async def capabilities(self, principal: Principal, vehicle_id: str) -> Capabilities:
        await self.authorize(principal, vehicle_id, "vehicle:read")
        caps = await self.backend.capabilities(vehicle_id)
        return caps.model_copy(
            update={
                "climate": caps.climate
                and self.enable_control
                and "vehicle:climate" in principal.scopes,
                "location": caps.location and "vehicle:location" in principal.scopes,
            }
        )

    async def state(self, principal: Principal, vehicle_id: str) -> VehicleState:
        await self.authorize(principal, vehicle_id, "vehicle:read")
        return await self.backend.state(vehicle_id)

    async def location(self, principal: Principal, vehicle_id: str) -> Location:
        await self.authorize(principal, vehicle_id, "vehicle:location")
        if not (await self.backend.capabilities(vehicle_id)).location:
            raise ServiceError("unsupported_capability")
        return await self.backend.location(vehicle_id)

    async def validate_control(self, command: ClimateCommand) -> None:
        if not self.enable_control:
            raise ServiceError("control_disabled")
        caps = await self.backend.capabilities(command.vehicle_id)
        if not caps.model_known or not caps.climate:
            raise ServiceError("unsupported_capability")
        if command.temperature_c is not None and (
            caps.temperature_min_c is None
            or caps.temperature_max_c is None
            or not caps.temperature_min_c <= command.temperature_c <= caps.temperature_max_c
            or caps.temperature_step_c is None
            or caps.temperature_step_c <= 0
            or (command.temperature_c - caps.temperature_min_c) % caps.temperature_step_c
        ):
            raise ServiceError("unsupported_temperature")

    async def submit(self, principal: Principal, command: ClimateCommand) -> Operation:
        # Validate even for direct business-layer callers, before idempotency lookup.
        command = ClimateCommand.model_validate(command.model_dump())
        if not self.enable_control:
            raise ServiceError("control_disabled")
        await self.authorize(principal, command.vehicle_id, "vehicle:climate")
        await self.validate_control(command)
        existing = self.store.existing(principal.subject, command)
        if existing:
            return existing
        if self._closing:
            raise ServiceError("service_stopping")
        if len(self._tasks) >= self.max_pending:
            raise ServiceError("queue_full")
        if self.store.unresolved(command.vehicle_id):
            raise ServiceError("vehicle_has_unknown_operation")
        op = self.store.create(principal.subject, command, simulated=self.backend.simulated)
        task = asyncio.create_task(self._execute(principal, command, op.operation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    async def _execute(
        self, principal: Principal, command: ClimateCommand, operation_id: str
    ) -> None:
        dispatch_started = False
        try:
            async with self._locks.setdefault(command.vehicle_id, asyncio.Lock()):
                # Recheck queued work after previous command, including membership and unknowns.
                await self.authorize(principal, command.vehicle_id, "vehicle:climate")
                await self.validate_control(command)
                if self.store.unresolved(command.vehicle_id):
                    raise ServiceError("vehicle_has_unknown_operation")
                self.store.transition(operation_id, Phase.RUNNING)
                started = now()
                async with asyncio.timeout(self.command_timeout):
                    # There is exactly one send; timeouts and renewal failures never replay it.
                    dispatch_started = True
                    try:
                        receipt = await self.backend.submit_climate(command)
                    except CommandRejected:
                        # Only this narrow submission contract proves non-acceptance.
                        dispatch_started = False
                        raise
                    started = now()  # Require a new sample after the submission response.
                    while True:
                        result = cloud_result(await self.backend.result(receipt))
                        if result == "unknown":
                            raise RuntimeError("conflicting_cloud_result")
                        if result == "failed":
                            self.store.transition(operation_id, Phase.FAILED, "cloud_rejected")
                            logger.info("operation_failed")
                            return
                        if result == "completed":
                            break
                        await asyncio.sleep(0.05)
                    self.store.transition(operation_id, Phase.CLOUD_COMPLETED)
                    while True:
                        state = await self.backend.state(command.vehicle_id)
                        enabled = state.signals.get("climate_enabled")
                        temp = state.signals.get("climate_target_c")
                        required = [enabled] if not command.enabled else [enabled, temp]
                        fresh = all(
                            s is not None
                            and not s.stale
                            and s.sampled_at is not None
                            and s.sampled_at >= started
                            for s in required
                        )
                        if (
                            fresh
                            and enabled is not None
                            and enabled.value is command.enabled
                            and (
                                not command.enabled
                                or (temp is not None and temp.value == command.temperature_c)
                            )
                        ):
                            self.store.transition(operation_id, Phase.VEHICLE_CONFIRMED)
                            logger.info("operation_confirmed")
                            return
                        await asyncio.sleep(0.05)
        except ServiceError as exc:
            if dispatch_started:
                # Membership, permission or result lookup failures cannot undo a sent command.
                self.store.transition(operation_id, Phase.UNKNOWN, "result_unknown_no_retry")
                logger.info("operation_unknown")
            else:
                self.store.transition(operation_id, Phase.FAILED, exc.code)
                logger.info("operation_failed")
        except asyncio.CancelledError:
            self.store.transition(operation_id, Phase.UNKNOWN, "interrupted_no_replay")
            raise
        except Exception:
            # Includes send/poll/confirmation timeout: acceptance cannot safely be disproved.
            self.store.transition(operation_id, Phase.UNKNOWN, "result_unknown_no_retry")
            logger.info("operation_unknown")

    async def operation(self, principal: Principal, operation_id: str) -> Operation:
        self.scope(principal, "vehicle:climate")
        op = self.store.get(operation_id, principal.subject)
        await self.authorize(principal, op.vehicle_id, "vehicle:climate")
        return op

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.backend.close()
        finally:
            self.store.close()
