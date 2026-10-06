from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def now() -> datetime:
    return datetime.now(UTC)


VehicleId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
IdempotencyKey = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[\w-]+$")]
Scope = Literal["vehicle:read", "vehicle:location", "vehicle:climate"]


class Principal(Model):
    subject: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    vehicle_ids: frozenset[VehicleId]
    scopes: frozenset[Scope]


class Vehicle(Model):
    vehicle_id: VehicleId
    model: str
    label: str
    simulated: bool = True


class Capabilities(Model):
    model_known: bool
    status: bool = True
    location: bool = False
    climate: bool = False
    charging_control: bool = False
    pet_mode: bool = False
    temperature_min_c: int | None = None
    temperature_max_c: int | None = None
    temperature_step_c: int | None = None
    simulated: bool = True


class Sample(Model):
    value: float | int | bool | str | None
    unit: str | None = None
    sampled_at: datetime | None
    stale: bool


class VehicleState(Model):
    vehicle_id: VehicleId
    observed_at: datetime
    signals: dict[str, Sample]
    stale: bool
    simulated: bool = True


class Location(Model):
    vehicle_id: VehicleId
    latitude: float | None
    longitude: float | None
    coordinate_system: str = "WGS84"
    sampled_at: datetime | None
    stale: bool
    simulated: bool = True


class ClimateCommand(Model):
    # No actor, account, arbitrary payload or model-supplied confirmation field.
    vehicle_id: VehicleId
    enabled: bool = Field(strict=True)
    temperature_c: int | None = Field(default=None, strict=True, ge=16, le=30)
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def check_temperature(self) -> ClimateCommand:
        if self.enabled and self.temperature_c is None:
            raise ValueError("temperature_required_when_enabling")
        if not self.enabled and self.temperature_c is not None:
            raise ValueError("temperature_not_allowed_when_disabling")
        return self


class Phase(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    CLOUD_COMPLETED = "cloud_completed"
    VEHICLE_CONFIRMED = "vehicle_confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OperationEvent(Model):
    phase: Phase
    at: datetime


class Operation(Model):
    operation_id: str
    vehicle_id: VehicleId
    phase: Phase
    created_at: datetime
    updated_at: datetime
    events: list[OperationEvent]
    error_code: str | None = None
    simulated: bool = True


class ServiceError(Exception):
    """Only fixed public error codes, never upstream error bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommandRejected(ServiceError):
    """Submission-only evidence: no command was sent, or the cloud explicitly rejected it.

    A lookup/confirmation error after submission must never imply this guarantee.
    """
