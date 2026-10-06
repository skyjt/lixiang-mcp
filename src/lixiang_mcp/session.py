"""Vehicle login boundary, deliberately unrelated to MCP gateway credentials."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class VehicleSession:
    access_token: str = field(repr=False)
    expires_at_monotonic: float


class LoginProvider(Protocol):
    async def renew(self, account_id: str) -> VehicleSession: ...


class SigningProvider(Protocol):
    async def sign(self, *, method: str, path: str, body: bytes) -> dict[str, str]: ...


class UnconfiguredLogin:
    async def renew(self, account_id: str) -> VehicleSession:
        raise RuntimeError("real_vehicle_login_not_implemented")


class UnconfiguredSigner:
    async def sign(self, *, method: str, path: str, body: bytes) -> dict[str, str]:
        raise RuntimeError("real_vehicle_signing_not_implemented")


class SessionManager:
    def __init__(self, provider: LoginProvider) -> None:
        self.provider = provider
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, VehicleSession] = {}

    async def get(self, account_id: str) -> VehicleSession:
        async with self._locks.setdefault(account_id, asyncio.Lock()):
            existing = self._sessions.get(account_id)
            if existing and existing.expires_at_monotonic > time.monotonic() + 30:
                return existing
            renewed = await self.provider.renew(account_id)
            self._sessions[account_id] = renewed
            return renewed
