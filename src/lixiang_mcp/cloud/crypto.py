"""PAKE proof and x-chj signing adapted from ha-lixiang v1.3.2.

Copyright (c) 2026 ha-lixiang contributors. MIT: licenses/ha-lixiang-MIT.txt.
No upstream signing key or device identity is included.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from typing import Any

import bcrypt
from nacl.signing import SigningKey

from .config import AccountSecrets, Profile
from .transport import ProtocolError


def create_seed(password: str) -> str:
    modulus = (1 << 128) - 159
    return format(int.from_bytes(hashlib.sha256(password.encode()).digest()) % modulus, "032x")


def bcrypt_base64(data: bytes) -> str:
    alphabet = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    bits = 0
    accumulator = 0
    result = ""
    for byte in data:
        accumulator = (accumulator << 8) | byte
        bits += 8
        while bits >= 6:
            bits -= 6
            result += alphabet[(accumulator >> bits) & 63]
    if bits:
        result += alphabet[(accumulator << (6 - bits)) & 63]
    return result


def create_proof(
    password: str, challenge: dict[str, Any], *, cnonce: str | None = None
) -> dict[str, str]:
    """Bound the server-selected work factor; unsupported challenges require operator handling."""
    try:
        option = challenge.get("option") or challenge.get("kdf")
        snonce = challenge.get("snonce")
        if not isinstance(option, str) or not isinstance(snonce, str) or len(snonce) > 256:
            raise ValueError
        head = option.removeprefix("bcrypt")
        if not re.fullmatch(r"\$2[aby]\$(0[4-9]|1[0-2])\$", head):
            raise ValueError
        salted = challenge.get("salted")
        if salted:
            if not isinstance(salted, str) or len(salted) > 128:
                raise ValueError
            salt = bytes(b for b in bytes.fromhex(salted) if b).decode("ascii")
        else:
            seeded = challenge.get("seeded")
            if not isinstance(seeded, str) or not re.fullmatch(r"[0-9a-fA-F]{1,32}", seeded):
                raise ValueError
            salt = bcrypt_base64(int(seeded, 16).to_bytes(16))
        if not re.fullmatch(r"[./A-Za-z0-9]{22}", salt):
            raise ValueError
        suffix = snonce.split(":")[-1]
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,64}", suffix):
            raise ValueError
        nonce = cnonce if cnonce is not None else secrets.token_hex(16)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", nonce):
            raise ValueError
        if not 1 <= len(password.encode()) <= 72:
            raise ValueError
        derived = bcrypt.hashpw(password.encode(), (head + salt).encode())
        key = SigningKey(hashlib.sha256(derived).digest())
        message = hashlib.sha256(bytes.fromhex(suffix) + bytes.fromhex(nonce)).digest()
        return {"cnonce": nonce, "snonce": snonce, "proof": key.sign(message).signature.hex()}
    except (ValueError, TypeError, UnicodeError):
        raise ProtocolError("unsupported_login_challenge") from None


class Signer:
    def __init__(self, profile: Profile, account: AccountSecrets) -> None:
        self.profile, self.account = profile, account

    def headers(
        self,
        method: str,
        body: bytes,
        *,
        bearer: str,
        vin: str = "",
        timestamp: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        timestamp = timestamp or str(int(time.time() * 1000))
        nonce = nonce or str(uuid.uuid4())
        md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
        fields = [
            "prod",
            self.profile.sign_app_version,
            self.account.key_id.get_secret_value(),
            self.account.device_id.get_secret_value(),
            method.upper(),
            "*/*",
            "zh-Hans-CN",
            md5,
            "application/json",
            timestamp,
            nonce,
        ]
        signature = base64.b64encode(
            hmac.new(
                bytes.fromhex(self.account.hac_key_hex.get_secret_value()),
                ("\n".join(fields) + "\n").encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return {
            "X-CHJ-Env": "prod",
            "X-CHJ-APP-Version": self.profile.sign_app_version,
            "X-CHJ-Key": self.account.key_id.get_secret_value(),
            "X-CHJ-Deviceid": self.account.device_id.get_secret_value(),
            "X-CHJ-Timestamp": timestamp,
            "X-CHJ-Nonce": nonce,
            "X-CHJ-Sign": signature,
            "Content-MD5": md5,
            "Content-Type": "application/json",
            "Content-Language": "zh-Hans-CN",
            "Accept": "*/*",
            "X-CHJ-Version": self.profile.sign_app_version,
            "X-CHJ-DeviceType": "2",
            "X-CHJ-ModelName": "IOS",
            "X-CHJ-Tag": "1",
            "X-CHJ-TOKEN": self.account.app_token.get_secret_value(),
            "X-CHJ-VIN": vin,
            "Authorization": f"Bearer {bearer}",
            "User-Agent": self.profile.api_user_agent,
        }
