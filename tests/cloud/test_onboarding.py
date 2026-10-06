import asyncio
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from lixiang_mcp.cloud.auth import AuthSession, CachedToken
from lixiang_mcp.cloud.backend import CloudBackend
from lixiang_mcp.cloud.config import SigningMaterial, load_cloud_config
from lixiang_mcp.cloud.profiles import builtin_profile
from lixiang_mcp.cloud.session_file import SessionFile
from lixiang_mcp.cloud.transport import ACCOUNT, ID, CloudHTTP, ProtocolError
from lixiang_mcp.config import AuthConfig, Settings
from lixiang_mcp.models import ClimateCommand, Phase, ServiceError
from lixiang_mcp.private_files import private_read, private_write, secret_json
from lixiang_mcp.service import VehicleService
from lixiang_mcp.setup.storage import SetupStore
from lixiang_mcp.setup.web import create_setup_app
from lixiang_mcp.setup.wizard import Action, Wizard
from lixiang_mcp.store import OperationStore


def new_wizard(config, cloud, directory):
    return Wizard(SetupStore(directory), profile=config.profile, transport_factory=cloud.transport)


async def act(wizard, name, **kwargs):
    return await wizard.act(Action(action=name, revision=wizard.state.revision, **kwargs))


async def settle(wizard):
    if wizard._task:
        await wizard._task
    assert wizard.state.phase not in {"authenticating", "discovering"}
    return wizard.status()


async def start(wizard, config, index=0):
    account = config.accounts[index]
    await act(wizard, "start", phone=account.phone, password=account.password)
    return await settle(wizard)


def material(config, index=0):
    return SigningMaterial.model_validate(
        config.accounts[index].model_dump(
            include={
                "device_id",
                "hac_key_hex",
                "key_id",
                "app_token",
            }
        )
    )


async def prepare_selection(wizard, config):
    await start(wizard, config)
    await act(wizard, "import_signing", signing=material(config))
    assert wizard.state.phase == "device_change_required"
    await act(wizard, "use_device")
    await settle(wizard)
    assert wizard.state.phase == "select_vehicles"


def test_builtin_profile_has_public_parameters_and_no_device_or_signing_secret():
    profile = builtin_profile()
    assert profile.profile_id == "ha-lixiang-v1.3.2"
    assert profile.redirect_uri == "https://account.lixiang.com/app-auth"
    assert profile.login_scope == "iam:client:type:app openid"
    assert not set(type(profile).model_fields) & {"device_id", "hac_key_hex", "key_id", "app_token"}


async def test_first_login_needs_only_credentials_and_stops_without_signing(
    config, cloud, tmp_path
):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        status = await start(wizard, config)
        assert status["phase"] == "signing_required"
        assert cloud.login_count == 1
        assert all(r.url.host != "api-app.lixiang.com" for r in cloud.requests)
        assert wizard.state.account.device_id != config.accounts[0].device_id
        output = json.dumps(status)
        for secret in (
            config.accounts[0].phone,
            config.accounts[0].password,
            wizard.state.account.device_id,
            wizard.state.session.access_token,
        ):
            assert secret.get_secret_value() not in output
        sealed = (tmp_path / "setup/state.box").read_bytes()
        assert config.accounts[0].password.get_secret_value().encode() not in sealed
        assert not status["vehicles"] and not cloud.sends
    finally:
        await wizard.close()


async def test_official_h5_continue_same_identity_no_status_poll_login(config, cloud, tmp_path):
    cloud.login_redirect = config.profile.redirect_uri + "?require=SMS_CODE"
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        status = await start(wizard, config)
        assert status["phase"] == "verification_required"
        url = urlsplit(status["official_url"])
        params = parse_qs(url.query)
        assert url.scheme == "https" and url.netloc == "account.lixiang.com"
        device = wizard.state.account.device_id.get_secret_value()
        assert params["device_id"] == [device]
        assert params["mode"] == ["h5"]
        assert not {"phone", "password", "code", "access_token"} & params.keys()
        count = len(cloud.requests)
        for _ in range(10):
            wizard.status()
        assert len(cloud.requests) == count and cloud.login_count == 1
        cloud.login_redirect = None
        await act(wizard, "continue")
        assert (await settle(wizard))["phase"] == "signing_required"
        assert wizard.state.account.device_id.get_secret_value() == device
        assert cloud.login_count == 2
    finally:
        await wizard.close()


async def test_duplicate_actions_only_one_login_and_restarts_preserve_budget(
    config, cloud, tmp_path
):
    cloud.login_redirect = config.profile.redirect_uri + "?require=SMS_CODE"
    directory = tmp_path / "setup"
    wizard = new_wizard(config, cloud, directory)
    command = Action(
        action="start",
        revision=0,
        phone=config.accounts[0].phone,
        password=config.accounts[0].password,
    )
    outcomes = await asyncio.gather(
        *(wizard.act(command) for _ in range(12)), return_exceptions=True
    )
    assert sum(isinstance(x, dict) for x in outcomes) == 1
    await settle(wizard)
    device = wizard.state.account.device_id
    await wizard.close()
    wizard = new_wizard(config, cloud, directory)
    try:
        assert cloud.login_count == 1 and wizard.state.account.device_id == device
        for _ in range(2):
            await act(wizard, "continue")
            await settle(wizard)
        assert cloud.login_count == 3
        await act(wizard, "continue")
        assert wizard.state.phase == "blocked"
        await wizard.close()
        wizard = new_wizard(config, cloud, directory)
        await act(wizard, "retry")
        assert cloud.login_count == 3 and wizard.status()["attempts_remaining"] == 0
    finally:
        await wizard.close()


async def test_cancel_during_login_and_restart_do_not_resurrect_credentials(
    config, cloud, tmp_path, monkeypatch
):
    entered = asyncio.Event()
    original = cloud.handle

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()
        return await original(request)

    monkeypatch.setattr(cloud, "handle", blocked)
    directory = tmp_path / "setup"
    wizard = new_wizard(config, cloud, directory)
    await act(wizard, "start", phone=config.accounts[0].phone, password=config.accounts[0].password)
    await entered.wait()
    device = wizard.state.account.device_id
    await act(wizard, "cancel")
    assert wizard.state.phase == "cancelled" and wizard.state.account is None
    assert wizard.state.session is None and wizard.state.signing is None
    await wizard.close()
    monkeypatch.setattr(cloud, "handle", original)
    wizard = new_wizard(config, cloud, directory)
    try:
        assert wizard.state.account is None and not cloud.requests
        await start(wizard, config)
        assert wizard.state.account.device_id == device
    finally:
        await wizard.close()


async def test_interrupted_task_requires_explicit_continue(config, cloud, tmp_path, monkeypatch):
    entered = asyncio.Event()
    original = cloud.handle

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()
        return await original(request)

    monkeypatch.setattr(cloud, "handle", blocked)
    directory = tmp_path / "setup"
    wizard = new_wizard(config, cloud, directory)
    await act(wizard, "start", phone=config.accounts[0].phone, password=config.accounts[0].password)
    await entered.wait()
    device = wizard.state.account.device_id
    await wizard.close()
    monkeypatch.setattr(cloud, "handle", original)
    wizard = new_wizard(config, cloud, directory)
    try:
        assert wizard.state.phase == "interrupted" and not cloud.requests
        await act(wizard, "continue")
        await settle(wizard)
        assert wizard.state.account.device_id == device and cloud.login_count == 1
    finally:
        await wizard.close()


async def test_signing_device_reauthentication_selection_and_export_readonly(
    config, cloud, tmp_path
):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        await prepare_selection(wizard, config)
        assert cloud.login_count == 2 and len(wizard.state.candidates) == 2
        assert wizard.state.account.device_id == config.accounts[0].device_id
        with pytest.raises(ServiceError, match="invalid_vehicle_selection"):
            await act(wizard, "select", selected=["foreign-car"])
        selected = wizard.state.candidates[1].vehicle_id
        await act(wizard, "select", selected=[selected])
        settings = Settings.model_validate_json(
            private_read(wizard.store.directory / wizard.state.export / "server.json")
        )
        exported = load_cloud_config(settings.vehicle_secrets_file)
        auth = AuthConfig.model_validate_json(private_read(settings.auth_file))
        principal = auth.credentials[0].principal
        assert exported.vehicles[0].vehicle_id == selected and len(exported.vehicles) == 1
        assert not settings.enable_control and not exported.allow_real_control
        assert (
            not exported.vehicles[0].climate_supported
            and not exported.vehicles[0].location_supported
        )
        assert principal.scopes == {"vehicle:read"} and principal.vehicle_ids == {selected}
        before = cloud.login_count
        backend = CloudBackend(exported, transport_factory=cloud.transport)
        service = VehicleService(backend, OperationStore(settings.database))
        try:
            assert [v.vehicle_id for v in await service.vehicles(principal)] == [selected]
            battery = (await service.state(principal, selected)).signals["battery_percent"]
            assert battery.value == 40 and not battery.stale
            assert cloud.login_count == before  # Persisted cookie/token/device set is reused.
            with pytest.raises(ServiceError, match="control_disabled"):
                await service.submit(
                    principal,
                    ClimateCommand(
                        vehicle_id=selected, enabled=False, idempotency_key="should-never-send"
                    ),
                )
            with pytest.raises(ServiceError, match="permission_denied"):
                await service.location(principal, selected)
            with pytest.raises(ServiceError, match="vehicle_not_found"):
                await service.state(principal, wizard.state.candidates[0].vehicle_id)
            with pytest.raises(ServiceError, match="stop_mcp_before_reconnecting"):
                await act(
                    wizard,
                    "start",
                    phone=config.accounts[0].phone,
                    password=config.accounts[0].password,
                )
            assert not cloud.sends
        finally:
            await service.close()
        for file in (wizard.store.directory / wizard.state.export).iterdir():
            if file.suffix == ".json" or file.name == "backend-token":
                assert file.stat().st_mode & 0o777 == 0o600
    finally:
        await wizard.close()


async def test_cancel_account_switch_does_not_reuse_session_material_or_candidates(
    config, cloud, tmp_path
):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        await prepare_selection(wizard, config)
        previous = wizard.state.account
        await act(wizard, "cancel")
        second = config.model_copy(
            update={
                "accounts": [
                    config.accounts[0],
                    config.accounts[1].model_copy(update={"phone": SecretStr("+" + "0" * 6 + "1")}),
                ]
            }
        )
        await start(wizard, second, index=1)
        assert wizard.state.account.account_id != previous.account_id
        assert wizard.state.account.device_id != previous.device_id
        assert not wizard.state.candidates and wizard.state.signing is None
        assert len(wizard.state.identities) == 2
        assert wizard.state.phase == "signing_required"
    finally:
        await wizard.close()


@pytest.mark.parametrize("fault", ["wrong_password", "invalid_redirect", "network"])
async def test_login_failure_needs_explicit_retry_and_sanitizes_errors(
    config, cloud, tmp_path, monkeypatch, fault
):
    original = cloud.handle

    async def handle(request):
        if request.url.path == "/api/login":
            if fault == "wrong_password":
                return httpx.Response(401, text="sensitive-password-response")
            if fault == "invalid_redirect":
                return httpx.Response(200, headers={"location": "https://["})
            raise httpx.ReadTimeout("sensitive-password-response")
        return await original(request)

    monkeypatch.setattr(cloud, "handle", handle)
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        status = await start(wizard, config)
        assert status["phase"] == "login_failed"
        assert "sensitive-password-response" not in json.dumps(status)
        assert cloud.login_count == 1
        for _ in range(5):
            wizard.status()
        assert cloud.login_count == 1
        monkeypatch.setattr(cloud, "handle", original)
        await act(wizard, "retry")
        assert (await settle(wizard))["phase"] == "signing_required"
    finally:
        await wizard.close()


async def test_snapshot_identity_or_profile_mismatch_is_rejected(config, cloud, tmp_path):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        await start(wizard, config)
        wizard._change(
            session=wizard.state.session.model_copy(update={"profile_digest": "0" * 64}),
            phase="interrupted",
        )
        before = len(cloud.requests)
        await act(wizard, "continue")
        status = await settle(wizard)
        assert status["error"] == "saved_session_identity_mismatch"
        assert len(cloud.requests) == before
    finally:
        await wizard.close()


async def test_snapshot_cannot_cross_accounts_on_the_same_device(config, cloud, tmp_path):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    http = CloudHTTP(frozenset({ID, ACCOUNT}), transport=cloud.transport())
    try:
        await start(wizard, config)
        other = wizard.state.account.model_copy(update={"phone": SecretStr("+" + "0" * 6 + "1")})
        before = len(cloud.requests)
        with pytest.raises(ProtocolError, match="saved_session_identity_mismatch"):
            AuthSession(config.profile, other, http, saved=wizard.state.session)
        assert len(cloud.requests) == before
    finally:
        await http.close()
        await wizard.close()


async def test_local_http_ui_security_duplicates_and_no_secret_echo(config, cloud, tmp_path):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    token = "synthetic-local-capability-" + "x" * 32
    app = create_setup_app(wizard, token)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 30000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        try:
            page = await client.get("/")
            assert page.status_code == 200 and "验证码只在理想官网输入" in page.text
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert token not in page.text and page.headers["cache-control"] == "no-store"
            assert (await client.get("/api/status")).status_code == 401
            headers = {"Authorization": "Bearer " + token, "Origin": "http://127.0.0.1:8765"}
            assert (
                await client.get(
                    "/api/status", headers={**headers, "Origin": "https://evil.invalid"}
                )
            ).status_code == 403
            assert (
                await client.get("/api/status", headers={**headers, "Host": "evil.invalid"})
            ).status_code == 403
            assert (
                await client.get("/api/status?token=anything", headers=headers)
            ).status_code == 403
            data = {
                "action": "start",
                "revision": 0,
                "phone": config.accounts[0].phone.get_secret_value(),
                "password": config.accounts[0].password.get_secret_value(),
            }
            assert (
                await client.post(
                    "/api/action", json=data, headers={"Authorization": "Bearer " + token}
                )
            ).status_code == 403
            assert (
                await client.post("/api/action", json={**data, "confirmed": True}, headers=headers)
            ).status_code == 400
            assert (
                await client.post(
                    "/api/action",
                    content=b"x" * 16385,
                    headers={**headers, "Content-Type": "application/json"},
                )
            ).status_code == 413
            responses = await asyncio.gather(
                *(client.post("/api/action", json=data, headers=headers) for _ in range(8))
            )
            assert sum(r.status_code == 200 for r in responses) == 1
            await settle(wizard)
            response = await client.get("/api/status", headers=headers)
            assert response.json()["phase"] == "signing_required" and cloud.login_count == 1
            assert data["phone"] not in response.text and data["password"] not in response.text
            assert (await client.get("/mcp", headers=headers)).status_code == 404
        finally:
            await wizard.close()


def test_private_storage_permissions_symlinks_lock_and_corruption(tmp_path):
    directory = tmp_path / "setup"
    store = SetupStore(directory)
    try:
        assert directory.stat().st_mode & 0o777 == 0o700
        with pytest.raises(BlockingIOError):
            SetupStore(directory)
        target = directory / "private.json"
        private_write(target, b"synthetic")
        target.chmod(0o644)
        with pytest.raises(ValueError, match="must_be_private"):
            private_read(target)
        target.chmod(0o600)
        link = directory / "link"
        link.symlink_to(target)
        with pytest.raises(OSError):
            private_read(link)
        private_write(directory / "state.box", b"corrupt")
        with pytest.raises(ValueError, match="checkpoint_invalid"):
            store.read()
        assert target.read_bytes() == b"synthetic"
    finally:
        store.close()
    os.chmod(directory, 0o755)
    with pytest.raises(ValueError, match="must_be_private"):
        SetupStore(directory)


async def test_session_rotation_persists_and_restarts_without_password_login(
    config, cloud, tmp_path
):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "protocol.json"
    private_write(path, secret_json(config))
    writer = SessionFile(path, config)
    backend = CloudBackend(
        config, transport_factory=cloud.transport, on_session_update=writer.update
    )
    try:
        await backend.vehicles("account-a")
        auth = backend._apis["account-a"].auth
        auth._main = CachedToken("expired", 0)
        auth._scopes.clear()
        await backend.vehicles("account-a")
        saved = load_cloud_config(path)
        assert (
            saved.accounts[0].saved_session.refresh_token.get_secret_value()
            == "synthetic-refresh-1"
        )
        assert saved.accounts[1].saved_session is None
        assert cloud.login_count == 1 and cloud.refresh_count == 1
    finally:
        await backend.close()
    backend = CloudBackend(saved, transport_factory=cloud.transport)
    try:
        await backend.vehicles("account-a")
        assert cloud.login_count == 1 and cloud.refresh_count == 1
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        await backend.close()


async def test_session_persistence_refuses_changed_config_without_leaking(config, cloud, tmp_path):
    path = tmp_path / "protocol.json"
    private_write(path, secret_json(config))
    writer = SessionFile(path, config)
    backend = CloudBackend(config, transport_factory=cloud.transport)
    try:
        await backend.vehicles("account-a")
        snapshot = await backend._apis["account-a"].auth.snapshot()
        changed = config.model_copy(update={"allow_real_control": False})
        private_write(path, secret_json(changed))
        with pytest.raises(ProtocolError, match="private_session_persistence_failed"):
            await writer.update("account-a", snapshot)
        assert not load_cloud_config(path).allow_real_control
    finally:
        await backend.close()


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_session_write_cannot_overwrite_a_later_rotation(
    config, cloud, tmp_path, monkeypatch, cancel_count
):
    path = tmp_path / "protocol.json"
    private_write(path, secret_json(config))
    writer = SessionFile(path, config)
    backend = CloudBackend(config, transport_factory=cloud.transport)
    release, entered = threading.Event(), threading.Event()
    tasks = []
    try:
        await backend.vehicles("account-a")
        snapshot = await backend._apis["account-a"].auth.snapshot()
        original = writer._write
        calls = []

        def delayed(account, session):
            calls.append(session)
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=5)
            original(account, session)

        monkeypatch.setattr(writer, "_write", delayed)
        first = asyncio.create_task(writer.update("account-a", snapshot))
        tasks.append(first)
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(cancel_count):
            first.cancel()
            await asyncio.sleep(0)
        newer = snapshot.model_copy(update={"refresh_token": SecretStr("synthetic-newest-refresh")})
        second = asyncio.create_task(writer.update("account-a", newer))
        tasks.append(second)
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
        assert (
            load_cloud_config(path).accounts[0].saved_session.refresh_token == newer.refresh_token
        )
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await backend.close()


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_scope_persistence_failure_blocks_account_without_cached_retry(
    config, cloud, tmp_path, monkeypatch, cancel_count
):
    path = tmp_path / "protocol.json"
    private_write(path, secret_json(config))
    writer = SessionFile(path, config)
    backend = CloudBackend(
        config, transport_factory=cloud.transport, on_session_update=writer.update
    )
    release, entered = threading.Event(), threading.Event()
    task = None
    try:
        await backend.vehicles("account-a")
        backend._apis["account-a"].auth._scopes.clear()

        def fail_write(account, session):
            entered.set()
            release.wait(timeout=5)
            raise OSError("synthetic-private-write-error")

        monkeypatch.setattr(writer, "_write", fail_write)
        task = asyncio.create_task(backend.vehicles("account-a"))
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, ProtocolError)
        assert outcome.code == "private_session_persistence_failed"
        assert "synthetic-private-write-error" not in str(outcome)
        requests = len(cloud.requests)
        with pytest.raises(ProtocolError, match="account_requires_operator_attention"):
            await backend.vehicles("account-a")
        assert len(cloud.requests) == requests
        assert not cloud.sends
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await backend.close()


async def test_cancel_reconnection_preserves_existing_configuration_and_database(
    config, cloud, tmp_path
):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        await prepare_selection(wizard, config)
        await act(wizard, "select", selected=[wizard.state.candidates[0].vehicle_id])
        directory = wizard.store.directory / wizard.state.export
        original = private_read(directory / "vehicle-protocol.json")
        cloud.login_redirect = config.profile.redirect_uri + "?require=SMS_CODE"
        await start(wizard, config)
        await act(wizard, "cancel")
        assert wizard.state.phase == "ready"
        assert private_read(directory / "vehicle-protocol.json") == original
        assert wizard.state.account.device_id == config.accounts[0].device_id
        assert wizard.state.session is not None
    finally:
        await wizard.close()


@pytest.mark.parametrize("cancel_before_restart", [False, True])
@pytest.mark.parametrize("missing_list", ["empty", "only_unselected"])
async def test_reconnection_recovers_committed_vehicle_identity_and_idempotency_history(
    config, cloud, tmp_path, monkeypatch, cancel_before_restart, missing_list
):
    directory = tmp_path / "setup"
    wizard = new_wizard(config, cloud, directory)
    try:
        await prepare_selection(wizard, config)
        chosen = wizard.state.candidates[0]
        await act(wizard, "select", selected=[chosen.vehicle_id])
        settings_path = Path(wizard.status()["config_file"])
        settings_bytes = private_read(settings_path)
        settings = Settings.model_validate_json(settings_bytes)
        committed = load_cloud_config(settings.vehicle_secrets_file)
        original_token = private_read(settings_path.parent / "backend-token")
        backend = CloudBackend(committed, transport_factory=cloud.transport)
        namespace = backend.storage_namespace
        await backend.close()
        command = ClimateCommand(
            vehicle_id=chosen.vehicle_id, enabled=False, idempotency_key="synthetic-prior-request"
        )
        journal = OperationStore(settings.database)
        try:
            journal.bind_backend(namespace)
            operation = journal.create("local-owner", command)
            journal.transition(operation.operation_id, Phase.UNKNOWN, "synthetic_unknown")
        finally:
            journal.close()
        database_bytes = settings.database.read_bytes()

        records = cloud.records["account-a"]
        cloud.records["account-a"] = [] if missing_list == "empty" else records[1:]
        await start(wizard, config)
        assert chosen.vehicle_id not in {c.vehicle_id for c in wizard.state.candidates}
        assert private_read(settings.vehicle_secrets_file) == secret_json(committed)
        if cancel_before_restart:
            requests = len(cloud.requests)
            await act(wizard, "cancel")
            assert wizard.state.phase == "ready"
            assert wizard.state.candidates == [chosen]
            assert wizard.state.selected == [chosen.vehicle_id]
            assert len(cloud.requests) == requests
        await wizard.close()
        wizard = new_wizard(config, cloud, directory)
        cloud.records["account-a"] = records
        # Advance the ordinary cooldown, without removing persisted attempt history.
        later = time.time() + 901
        monkeypatch.setattr("lixiang_mcp.setup.wizard.time.time", lambda: later)
        await start(wizard, config)
        recovered = next(c for c in wizard.state.candidates if c.vin == chosen.vin)
        assert recovered.vehicle_id == chosen.vehicle_id
        await act(wizard, "select", selected=[chosen.vehicle_id])
        assert wizard.state.phase == "ready"
        assert Path(wizard.status()["config_file"]) == settings_path
        assert private_read(settings_path) == settings_bytes
        assert private_read(settings_path.parent / "backend-token") == original_token
        assert settings.database.read_bytes() == database_bytes
        updated = load_cloud_config(settings.vehicle_secrets_file)
        assert [(v.vin, v.vehicle_id) for v in updated.vehicles] == [
            (chosen.vin, chosen.vehicle_id)
        ]
        backend = CloudBackend(updated, transport_factory=cloud.transport)
        assert backend.storage_namespace == namespace
        await backend.close()
        journal = OperationStore(settings.database)
        try:
            journal.bind_backend(namespace)
            assert journal.existing("local-owner", command).operation_id == operation.operation_id
            assert journal.unresolved(chosen.vehicle_id)
        finally:
            journal.close()
        assert not cloud.sends
    finally:
        await wizard.close()


async def test_expired_session_and_failed_refresh_recover_only_on_explicit_action(
    config, cloud, tmp_path
):
    wizard = new_wizard(config, cloud, tmp_path / "setup")
    try:
        await start(wizard, config)
        wizard._change(
            phase="interrupted", session=wizard.state.session.model_copy(update={"expires_at": 0})
        )
        cloud.refresh_error = True
        count = cloud.login_count
        assert wizard.status()["phase"] == "interrupted" and cloud.login_count == count
        await act(wizard, "continue")
        assert (await settle(wizard))["phase"] == "signing_required"
        assert cloud.login_count == count + 1 and cloud.refresh_count == 1
    finally:
        await wizard.close()
