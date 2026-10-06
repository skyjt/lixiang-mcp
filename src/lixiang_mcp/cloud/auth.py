"""Cookie/PKCE/PAKE account session adapted from ha-lixiang v1.3.2.

Copyright (c) 2026 ha-lixiang contributors. MIT: licenses/ha-lixiang-MIT.txt.
Uses external app identifiers and account secrets; never uses MCP gateway tokens.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import SplitResult, parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from .config import LoginCredentials, Profile, SavedCookie, SavedSession
from .crypto import create_proof, create_seed
from .transport import ACCOUNT, ID, CloudHTTP, ProtocolError, object_body


@dataclass(frozen=True)
class CachedToken:
    value: str = field(repr=False)
    expires_at: float


def split_redirect(location: str) -> SplitResult:
    try:
        return urlsplit(location)
    except ValueError:
        raise ProtocolError("invalid_auth_redirect") from None


def expiration(value: Any, maximum: int) -> float:
    # The upstream defaults are conservative cache bounds, never authorization claims.
    if value is None:
        seconds = maximum
    else:
        try:
            seconds = int(value)
        except (ValueError, TypeError, OverflowError):
            raise ProtocolError("invalid_token_lifetime") from None
        if isinstance(value, bool) or seconds <= 0:
            raise ProtocolError("invalid_token_lifetime")
        seconds = min(seconds, maximum)
    return time.monotonic() + max(0, seconds - min(30, seconds / 10))


class AuthSession:
    def __init__(
        self,
        profile: Profile,
        account: LoginCredentials,
        http: CloudHTTP,
        *,
        saved: SavedSession | None = None,
        login_limit: int | None = None,
        on_update: Callable[[SavedSession], Awaitable[None]] | None = None,
    ) -> None:
        self.profile, self.account, self.http = profile, account, http
        self._lock = asyncio.Lock()
        self._main: CachedToken | None = None
        self._refresh = ""
        self._scopes: dict[tuple[str, str], CachedToken] = {}
        self._blocked = False
        self._generation = 0
        self._login_limit, self._login_attempts = login_limit, 0
        self._on_update = on_update
        if saved is not None:
            self._restore(saved)

    def _profile_digest(self) -> str:
        return hashlib.sha256(self.profile.model_dump_json().encode()).hexdigest()

    def _account_digest(self) -> str:
        phone = self.account.phone.get_secret_value()
        phone = phone if phone.startswith("+") else "+86" + phone
        return hashlib.sha256((self.account.account_id + "\0" + phone).encode()).hexdigest()

    def _restore(self, saved: SavedSession) -> None:
        if (
            saved.device_id != self.account.device_id
            or saved.account_digest != self._account_digest()
            or saved.profile_digest != self._profile_digest()
        ):
            raise ProtocolError("saved_session_identity_mismatch")
        self._main = CachedToken(
            saved.access_token.get_secret_value(),
            time.monotonic() + min(3600, saved.expires_at - time.time()),
        )
        self._refresh = saved.refresh_token.get_secret_value()
        for item in saved.cookies:
            self.http.client.cookies.jar.set_cookie(
                Cookie(
                    0,
                    item.name,
                    item.value.get_secret_value(),
                    None,
                    False,
                    item.domain,
                    True,
                    item.domain.startswith("."),
                    item.path,
                    True,
                    item.secure,
                    item.expires,
                    item.expires is None,
                    None,
                    None,
                    {},
                    False,
                )
            )

    def _snapshot(self) -> SavedSession:
        if self._main is None:
            raise ProtocolError("account_session_missing")
        return SavedSession(
            device_id=self.account.device_id,
            account_digest=self._account_digest(),
            profile_digest=self._profile_digest(),
            access_token=SecretStr(self._main.value),
            refresh_token=SecretStr(self._refresh),
            expires_at=max(0, time.time() + self._main.expires_at - time.monotonic()),
            cookies=[
                SavedCookie(
                    name=c.name,
                    value=SecretStr(c.value or ""),
                    domain=c.domain,
                    path=c.path,
                    secure=c.secure,
                    expires=c.expires,
                )
                for c in self.http.client.cookies.jar
                if c.domain.lstrip(".") in {"id.lixiang.com", "account.lixiang.com"}
            ],
        )

    async def establish_session(self) -> SavedSession:
        """One explicit local setup attempt; does not fetch vehicles or bypass challenges."""
        async with self._lock:
            if self._blocked:
                raise ProtocolError("account_requires_operator_attention")
            try:
                await self._ensure_session()
                return self._snapshot()
            except ProtocolError:
                self._blocked = True
                raise

    async def snapshot(self) -> SavedSession:
        async with self._lock:
            return self._snapshot()

    async def _publish(self) -> None:
        if self._on_update is not None:
            await self._on_update(self._snapshot())

    def _headers(self) -> dict[str, str]:
        p, device = self.profile, self.account.device_id.get_secret_value()
        return {
            "idaas-data": f"model_name=OpenHarmony;device_model=;device_id={device};"
            f"app_version={p.login_app_version};client_id={p.client_id};"
            f"sdk_version={p.sdk_version};timestamp={int(time.time() * 1000)}",
            "idaas-data-x": "source_url=registerAction=&pageUrl=&eventID=",
            "Origin": ACCOUNT,
            "Referer": ACCOUNT + "/",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": p.login_user_agent,
        }

    def _location(self, response: httpx.Response) -> str:
        location = response.headers.get("location")
        if location is None:
            location = object_body(response).get("location")
        if not isinstance(location, str) or len(location) > 16384:
            raise ProtocolError("invalid_auth_redirect")
        return location

    def _redirect(self, location: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        parsed = split_redirect(location)
        configured = split_redirect(self.profile.redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            configured.scheme,
            configured.netloc,
            configured.path,
        ):
            raise ProtocolError("unexpected_auth_redirect")
        return parse_qs(parsed.query), parse_qs(parsed.fragment)

    def _save_main(self, body: dict[str, Any]) -> None:
        access = body.get("access_token")
        refresh = body.get("refresh_token", self._refresh)
        if not isinstance(access, str) or not access or len(access) > 16384:
            raise ProtocolError("missing_main_token")
        if not isinstance(refresh, str) or len(refresh) > 16384:
            raise ProtocolError("invalid_refresh_token")
        self._main = CachedToken(access, expiration(body.get("expires_in"), 3600))
        self._refresh = refresh

    async def _login(self) -> None:
        # Call only while holding the account lock. A new session discards stale cookies.
        if self._login_limit is not None and self._login_attempts >= self._login_limit:
            raise ProtocolError("explicit_login_retry_required")
        self._login_attempts += 1
        self.http.client.cookies.clear()
        self._main, self._refresh = None, ""
        self._scopes.clear()
        device = self.account.device_id.get_secret_value()
        for domain in ("id.lixiang.com", "account.lixiang.com"):
            for name in ("X-LX-Deviceid", "authli_device_id"):
                self.http.client.cookies.set(name, device, domain=domain, path="/")
        self.http.client.cookies.set("isapp", "1", domain="account.lixiang.com", path="/")
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(21)
        p = self.profile
        response = await self.http.request(
            "POST",
            ID + "/api/auth",
            headers=self._headers(),
            data={
                "client_id": p.client_id,
                "device_id": device,
                "response_type": "code",
                "redirect_uri": p.redirect_uri,
                "offline_access": "true",
                "state": state,
                "audience": p.login_audience,
                "scope": p.login_scope,
                "code_challenge": challenge.rstrip(b"=").decode(),
                "code_challenge_method": "S256",
            },
        )
        if response.status_code not in (200, 300):
            raise ProtocolError("login_auth_failed")
        page = httpx.URL(
            ACCOUNT + "/login", params={"client_id": p.client_id, "redirect_uri": p.redirect_uri}
        )
        response = await self.http.request(
            "GET",
            str(page),
            headers={
                **self._headers(),
                "Accept": "text/html",
                "Referer": ACCOUNT + "/app-auth",
            },
        )
        if response.status_code != 200:
            raise ProtocolError("login_page_failed")
        response = await self.http.request(
            "POST",
            ID + "/api/devices",
            headers=self._headers(),
            json={
                "client_id": p.client_id,
                "device_id": device,
                "user_agent": p.login_user_agent,
                "model": "",
                "manufacturer": "",
                "os": "OpenHarmony",
                "os_version": "6.1",
                "screen_height": 843,
                "screen_width": 374,
                "app_version": p.login_app_version,
            },
        )
        if response.status_code != 200:
            raise ProtocolError("device_registration_failed")
        phone = self.account.phone.get_secret_value()
        phone = phone if phone.startswith("+") else "+86" + phone
        password = self.account.password.get_secret_value()
        response = await self.http.request(
            "POST",
            ID + "/api/idps",
            headers=self._headers(),
            json={
                "origin": ACCOUNT,
                "connection": "LI_USER",
                "seed": create_seed(password),
                "strategy": "PASSWORD",
                "user_tip": phone,
            },
        )
        if response.status_code != 200:
            raise ProtocolError("login_challenge_required")
        challenge_data = object_body(response).get("t_login_use")
        if not isinstance(challenge_data, dict):
            raise ProtocolError("login_challenge_required")
        proof = await asyncio.to_thread(create_proof, password, challenge_data)
        response = await self.http.request(
            "POST",
            ID + "/api/login",
            headers=self._headers(),
            json={
                "client_id": p.client_id,
                "connection": "LI_USER",
                "user_tip": phone,
                "proof": proof,
            },
        )
        if response.status_code not in (200, 300, 302):
            raise ProtocolError("login_denied_or_challenge_required")
        location = self._location(response)
        if parse_qs(split_redirect(location).query).get("require"):
            raise ProtocolError("login_challenge_required")
        query, _ = self._redirect(location)
        if query.get("state") != [state] or len(query.get("code", [])) != 1 or not query["code"][0]:
            raise ProtocolError("login_state_or_code_invalid")
        response = await self.http.request(
            "POST",
            ID + "/api/token",
            headers=self._headers(),
            data={
                "client_id": p.client_id,
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "code_verifier": verifier,
            },
        )
        if response.status_code != 200:
            raise ProtocolError("token_exchange_failed")
        self._save_main(object_body(response))
        self._generation += 1
        self._scopes.clear()
        await self._publish()

    async def _ensure_session(self) -> None:
        if self._main is None:
            await self._login()
        elif self._main.expires_at <= time.monotonic():
            if not self._refresh:
                await self._login()
                return
            response = await self.http.request(
                "POST",
                ID + "/api/token",
                headers=self._headers(),
                data={
                    "client_id": self.profile.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh,
                },
            )
            if response.status_code in (400, 401):
                await self._login()
            elif response.status_code != 200:
                raise ProtocolError("token_refresh_failed")
            else:
                self._save_main(object_body(response))
                await self._publish()

    async def _exchange(self, audience: str, scope: str, ttl: int) -> CachedToken:
        response = await self.http.request(
            "POST",
            ID + "/api/auth",
            headers=self._headers(),
            data={
                "prompt": "none",
                "offline_access": "true",
                "redirect_uri": self.profile.redirect_uri,
                "scope": scope,
                "response_type": "token",
                "device_id": self.account.device_id.get_secret_value(),
                "audience": audience,
                "client_id": self.profile.client_id,
            },
        )
        if response.status_code == 401:
            raise ProtocolError("session_expired")
        if response.status_code not in (200, 300, 302):
            raise ProtocolError("scope_exchange_failed")
        location = self._location(response)
        # login_required is the only auto re-login condition, not any malformed/error response.
        parts = split_redirect(location)
        values = {**parse_qs(parts.query), **parse_qs(parts.fragment)}
        if values.get("error") == ["login_required"]:
            raise ProtocolError("session_expired")
        _, fragment = self._redirect(location)
        tokens = fragment.get("access_token", [])
        if len(tokens) != 1 or not tokens[0] or len(tokens[0]) > 16384:
            raise ProtocolError("scope_exchange_failed")
        granted = fragment.get("scope")
        if granted is not None and (
            len(granted) != 1 or not set(scope.split()) <= set(granted[0].split())
        ):
            raise ProtocolError("scope_not_granted")
        lifetimes = fragment.get("expires_in", [])
        if len(lifetimes) > 1:
            raise ProtocolError("invalid_token_lifetime")
        return CachedToken(tokens[0], expiration(lifetimes[0] if lifetimes else None, ttl))

    async def scope(self, audience: str, scope: str, *, ttl: int = 780) -> str:
        async with self._lock:
            return await self._scope_locked(audience, scope, ttl=ttl)

    async def bundle(self, specs: list[tuple[str, str, int]]) -> list[str]:
        """Get a coherent token set when an exchange may rebuild the cookie session."""
        async with self._lock:
            for _ in range(2):
                generation = self._generation
                values = [
                    await self._scope_locked(aud, scope, ttl=ttl) for aud, scope, ttl in specs
                ]
                if generation == self._generation:
                    return values
            raise ProtocolError("unstable_account_session")

    async def _scope_locked(self, audience: str, scope: str, *, ttl: int) -> str:
        if self._blocked:
            raise ProtocolError("account_requires_operator_attention")
        key = (audience, scope)
        existing = self._scopes.get(key)
        if existing and existing.expires_at > time.monotonic():
            return existing.value
        try:
            await self._ensure_session()
            try:
                result = await self._exchange(audience, scope, ttl)
            except ProtocolError as exc:
                if exc.code != "session_expired":
                    raise
                await self._login()
                result = await self._exchange(audience, scope, ttl)
            await self._publish()
            self._scopes[key] = result
            return result.value
        except ProtocolError as exc:
            if exc.code not in {"upstream_network_error", "token_refresh_failed"}:
                self._blocked = True
            raise

    async def invalidate(self, audience: str, scope: str, rejected_token: str) -> None:
        async with self._lock:
            key = (audience, scope)
            cached = self._scopes.get(key)
            if cached and secrets.compare_digest(cached.value, rejected_token):
                self._scopes.pop(key)
