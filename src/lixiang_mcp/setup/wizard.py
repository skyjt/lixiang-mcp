"""Explicit local human actions drive account onboarding; no MCP credential entry points."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import Field, SecretStr

from ..cloud.api import VehicleAPI
from ..cloud.auth import AuthSession
from ..cloud.backend import usable_relation
from ..cloud.config import (
    AccountSecrets,
    CloudConfig,
    LoginCredentials,
    Profile,
    SavedSession,
    SigningMaterial,
    VehicleBinding,
    load_cloud_config,
)
from ..cloud.crypto import Signer
from ..cloud.profiles import builtin_profile, official_verification_url
from ..cloud.transport import ACCOUNT, API, ID, CloudHTTP, ProtocolError
from ..models import Model, ServiceError, VehicleId
from ..private_files import private_directory, private_read, private_write, secret_json
from .storage import SetupStore

Phase = Literal[
    "new",
    "authenticating",
    "verification_required",
    "login_failed",
    "blocked",
    "signing_required",
    "device_change_required",
    "discovering",
    "select_vehicles",
    "ready",
    "interrupted",
    "cancelled",
]
BUSY = {"authenticating", "discovering"}
MAX_ATTEMPTS = 3
ATTEMPT_WINDOW = 900


class Identity(Model):
    account_id: VehicleId
    device_id: SecretStr


class Candidate(Model):
    vehicle_id: VehicleId
    vin: SecretStr
    model_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)


class Checkpoint(Model):
    version: Literal[1] = 1
    revision: int = 0
    phase: Phase = "new"
    profile: Profile = Field(default_factory=builtin_profile)
    identities: dict[str, Identity] = Field(default_factory=dict)
    attempts: dict[str, list[float]] = Field(default_factory=dict)
    account: LoginCredentials | None = None
    signing: SigningMaterial | None = None
    session: SavedSession | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    selected: list[VehicleId] = Field(default_factory=list)
    export: str | None = Field(default=None, pattern=r"^export-[a-f0-9]{32}$")
    error: str | None = None


class Action(Model):
    action: Literal[
        "start", "continue", "retry", "cancel", "import_signing", "use_device", "select"
    ]
    revision: int = Field(ge=0, strict=True)
    phone: SecretStr | None = None
    password: SecretStr | None = None
    signing: SigningMaterial | None = None
    selected: list[VehicleId] = Field(default_factory=list, max_length=100)


def normalize_phone(value: str) -> str:
    value = re.sub(r"[\s()-]", "", value)
    if re.fullmatch(r"1[3-9][0-9]{9}", value):
        return "+86" + value
    if re.fullmatch(r"861[3-9][0-9]{9}", value):
        return "+" + value
    if not re.fullmatch(r"\+[0-9]{6,20}", value):
        raise ServiceError("invalid_phone_format")
    return value


class Wizard:
    def __init__(
        self,
        store: SetupStore,
        *,
        profile: Profile | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self.store = store
        try:
            saved = store.read()
            self.state = (
                Checkpoint.model_validate(saved)
                if saved
                else Checkpoint(profile=profile or builtin_profile())
            )
        except Exception:
            store.close()
            raise
        self.transport_factory = transport_factory
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._closed = False
        if self.state.phase in BUSY:
            self._change(phase="interrupted", error="previous_attempt_interrupted")
        elif saved is None:
            self.store.write(self.state)

    def _change(self, **changes: Any) -> None:
        next_state = self.state.model_copy(update={**changes, "revision": self.state.revision + 1})
        self.store.write(next_state)
        self.state = next_state

    def _attempts(self) -> list[float]:
        account = self.state.account
        history = self.state.attempts.get(account.account_id, []) if account else []
        return [stamp for stamp in history if time.time() - stamp < ATTEMPT_WINDOW]

    def status(self) -> dict[str, Any]:
        state = self.state
        attempts = self._attempts()
        result: dict[str, Any] = {
            "phase": state.phase,
            "revision": state.revision,
            "error": state.error,
            "profile": state.profile.profile_id,
            "attempts_remaining": max(0, MAX_ATTEMPTS - len(attempts)),
            "retry_after_seconds": max(0, int(attempts[0] + ATTEMPT_WINDOW - time.time()) + 1)
            if len(attempts) >= MAX_ATTEMPTS
            else 0,
            "signing_ready": state.signing is not None,
            "vehicles": [
                {
                    "vehicle_id": c.vehicle_id,
                    "label": c.label,
                    "vin_tail": c.vin.get_secret_value()[-4:],
                    "selected": c.vehicle_id in state.selected,
                }
                for c in state.candidates
            ]
            if state.phase in {"select_vehicles", "ready"}
            else [],
        }
        if state.phase == "verification_required" and state.account:
            result["official_url"] = official_verification_url(
                state.profile, state.account.device_id.get_secret_value()
            )
        if state.phase == "ready" and state.export:
            result["config_file"] = str(self.store.directory / state.export / "server.json")
        return result

    def _ensure_service_stopped(self) -> None:
        if not self.state.export:
            return
        path = self.store.directory / self.state.export / "operations.lock"
        if path.exists():
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ServiceError("stop_mcp_before_reconnecting") from None
            finally:
                os.close(fd)

    def _launch(self) -> None:
        if self.state.account is None:
            raise ServiceError("account_required")
        attempts = self._attempts()
        if len(attempts) >= MAX_ATTEMPTS:
            self._change(phase="blocked", error="attempt_limit_reached")
            return
        history = {**self.state.attempts, self.state.account.account_id: [*attempts, time.time()]}
        self._generation += 1
        self._change(phase="authenticating", attempts=history, error=None)
        self._task = asyncio.create_task(self._run(self._generation))

    async def act(self, action: Action) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise ServiceError("setup_closed")
            if action.revision != self.state.revision:
                raise ServiceError("stale_setup_action")
            phase = self.state.phase
            if action.action == "cancel":
                self._generation += 1
                if self._task:
                    self._task.cancel()
                if self.state.export:
                    existing = load_cloud_config(
                        self.store.directory / self.state.export / "vehicle-protocol.json"
                    ).accounts[0]
                    account = LoginCredentials.model_validate(
                        existing.model_dump(
                            include={
                                "account_id",
                                "phone",
                                "password",
                                "device_id",
                            }
                        )
                    )
                    signing = SigningMaterial.model_validate(
                        existing.model_dump(
                            include={
                                "device_id",
                                "key_id",
                                "hac_key_hex",
                                "app_token",
                            }
                        )
                    )
                    key = hashlib.sha256(account.phone.get_secret_value().encode()).hexdigest()
                    identities = {
                        **self.state.identities,
                        key: Identity(
                            account_id=account.account_id,
                            device_id=account.device_id,
                        ),
                    }
                    self._change(
                        phase="ready",
                        account=account,
                        signing=signing,
                        session=existing.saved_session,
                        identities=identities,
                        error=None,
                    )
                    return self.status()
                self._change(
                    phase="cancelled",
                    account=None,
                    session=None,
                    signing=None,
                    candidates=[],
                    selected=[],
                    error=None,
                )
                return self.status()
            if phase in BUSY:
                raise ServiceError("setup_busy")
            self._ensure_service_stopped()
            if action.action == "start":
                if action.phone is None or action.password is None:
                    raise ServiceError("credentials_required")
                phone = normalize_phone(action.phone.get_secret_value())
                current = self.state.account
                if self.state.export and (
                    current is None or current.phone.get_secret_value() != phone
                ):
                    raise ServiceError("existing_connection_account_locked")
                key = hashlib.sha256(phone.encode()).hexdigest()
                identities = dict(self.state.identities)
                if key not in identities:
                    if len(identities) >= 20:
                        raise ServiceError("local_account_limit")
                    identities[key] = Identity(
                        account_id="account-" + uuid.uuid4().hex[:16],
                        device_id=SecretStr(uuid.uuid4().hex),
                    )
                identity = identities[key]
                account = LoginCredentials(
                    account_id=identity.account_id,
                    phone=SecretStr(phone),
                    password=action.password,
                    device_id=identity.device_id,
                )
                same = current is not None and current.account_id == account.account_id
                self._change(
                    account=account,
                    identities=identities,
                    session=None,
                    signing=self.state.signing if same else None,
                    candidates=self.state.candidates if same else [],
                    selected=self.state.selected if same else [],
                )
                self._launch()
            elif action.action in {"continue", "retry"}:
                if phase not in {"verification_required", "login_failed", "blocked", "interrupted"}:
                    raise ServiceError("invalid_setup_transition")
                self._launch()
            elif action.action == "import_signing":
                if phase not in {
                    "signing_required",
                    "device_change_required",
                    "login_failed",
                    "select_vehicles",
                }:
                    raise ServiceError("invalid_setup_transition")
                if action.signing is None or self.state.account is None:
                    raise ServiceError("signing_material_required")
                matching = action.signing.device_id == self.state.account.device_id
                self._change(
                    signing=action.signing,
                    phase="signing_required" if matching else "device_change_required",
                    error=None,
                    candidates=[] if not self.state.export else self.state.candidates,
                )
                if matching:
                    self._launch()
            elif action.action == "use_device":
                if (
                    phase != "device_change_required"
                    or not self.state.account
                    or not self.state.signing
                ):
                    raise ServiceError("invalid_setup_transition")
                account = self.state.account.model_copy(
                    update={"device_id": self.state.signing.device_id}
                )
                key = hashlib.sha256(account.phone.get_secret_value().encode()).hexdigest()
                identities = {
                    **self.state.identities,
                    key: Identity(
                        account_id=account.account_id,
                        device_id=account.device_id,
                    ),
                }
                self._change(account=account, identities=identities, session=None)
                self._launch()
            elif action.action == "select":
                if phase != "select_vehicles":
                    raise ServiceError("invalid_setup_transition")
                selected = set(action.selected)
                if (
                    not selected
                    or len(selected) != len(action.selected)
                    or not selected <= {c.vehicle_id for c in self.state.candidates}
                ):
                    raise ServiceError("invalid_vehicle_selection")
                if self.state.export and selected != set(self.state.selected):
                    raise ServiceError("existing_connection_selection_locked")
                self._export(action.selected)
            return self.status()

    async def _run(self, generation: int) -> None:
        state = self.state
        account = state.account
        assert account is not None
        http = CloudHTTP(
            frozenset({ID, ACCOUNT}),
            transport=self.transport_factory() if self.transport_factory else None,
        )
        api_http: CloudHTTP | None = None
        established: SavedSession | None = None
        try:
            async with asyncio.timeout(90):
                auth = AuthSession(state.profile, account, http, saved=state.session, login_limit=1)
                session = await auth.establish_session()
                established = session
                async with self._lock:
                    if generation != self._generation:
                        return
                    if state.signing is None:
                        self._change(phase="signing_required", session=session, error=None)
                        return
                    if state.signing.device_id != account.device_id:
                        self._change(phase="device_change_required", session=session, error=None)
                        return
                    self._change(phase="discovering", session=session)
                complete = AccountSecrets.model_validate(
                    {**account.model_dump(), **state.signing.model_dump()}
                )
                api_http = CloudHTTP(
                    frozenset({API}),
                    transport=self.transport_factory() if self.transport_factory else None,
                )
                api = VehicleAPI(state.profile, auth, Signer(state.profile, complete), api_http)
                records = await api.vehicles()
                candidates = self._candidates(records, state.candidates)
                session = await auth.snapshot()
                async with self._lock:
                    if generation == self._generation:
                        self._change(
                            phase="select_vehicles" if candidates else "login_failed",
                            session=session,
                            candidates=candidates,
                            error=None if candidates else "no_eligible_vehicles",
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, (ProtocolError, ServiceError))
                else "setup_connection_failed"
            )
            phase: Phase = (
                "verification_required" if code == "login_challenge_required" else "login_failed"
            )
            async with self._lock:
                if generation == self._generation:
                    self._change(
                        phase=phase,
                        session=established
                        if api_http and phase != "verification_required"
                        else None,
                        error=code,
                    )
        finally:
            await http.close()
            if api_http:
                await api_http.close()

    @staticmethod
    def _candidates(records: list[dict[str, Any]], previous: list[Candidate]) -> list[Candidate]:
        candidates, seen = [], set()
        old = {c.vin.get_secret_value(): c for c in previous}
        for record in records:
            vin, model = record.get("vin"), record.get("modelId")
            if (
                not usable_relation(record)
                or not isinstance(vin, str)
                or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin)
            ):
                continue
            if vin in seen:
                raise ProtocolError("ambiguous_vehicle_identity")
            seen.add(vin)
            model_id = str(model) if type(model) in (str, int) and str(model) else "UNVERIFIED"
            label = record.get("modelName")
            label = label[:64] if isinstance(label, str) and label else "我的车辆"
            candidates.append(
                Candidate(
                    vehicle_id=old[vin].vehicle_id
                    if vin in old
                    else "car-" + uuid.uuid4().hex[:16],
                    vin=SecretStr(vin),
                    model_id=model_id,
                    label=label,
                )
            )
        if len(candidates) > 100:
            raise ProtocolError("vehicle_list_too_large")
        return candidates

    def _export(self, selected: list[str]) -> None:
        state = self.state
        assert state.account and state.signing and state.session
        account = AccountSecrets.model_validate(
            {
                **state.account.model_dump(),
                **state.signing.model_dump(),
                "saved_session": state.session,
            }
        )
        config = CloudConfig(
            profile=state.profile,
            accounts=[account],
            vehicles=[
                VehicleBinding(
                    vehicle_id=c.vehicle_id,
                    account_id=account.account_id,
                    vin=c.vin,
                    label=c.label,
                    model_label=c.label,
                    model_id=c.model_id,
                )
                for c in state.candidates
                if c.vehicle_id in selected
            ],
        )
        export = state.export or "export-" + uuid.uuid4().hex
        directory = self.store.directory / export
        private_directory(directory)
        token_path = directory / "backend-token"
        token = (
            private_read(token_path, 512).decode()
            if token_path.exists()
            else secrets.token_urlsafe(32)
        )
        auth = {
            "credentials": [
                {
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "principal": {
                        "subject": "local-owner",
                        "account_id": account.account_id,
                        "vehicle_ids": selected,
                        "scopes": ["vehicle:read"],
                    },
                }
            ]
        }
        settings = {
            "backend": "lixiang",
            "vehicle_secrets_file": str(directory / "vehicle-protocol.json"),
            "auth_file": str(directory / "backend-auth.json"),
            "database": str(directory / "operations.sqlite"),
            "host": "127.0.0.1",
            "port": 8001,
            "allowed_hosts": ["127.0.0.1", "localhost"],
            "enable_control": False,
            "persist_vehicle_sessions": True,
            "command_timeout": 60,
        }
        private_write(directory / "vehicle-protocol.json", secret_json(config))
        private_write(token_path, token.encode())
        private_write(directory / "backend-auth.json", secret_json(auth))
        private_write(directory / "server.json", secret_json(settings))
        self._change(phase="ready", selected=selected, export=export, error=None)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            if self._task:
                self._task.cancel()
            if self.state.phase in BUSY:
                self._change(phase="interrupted", error="previous_attempt_interrupted")
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        self.store.close()
