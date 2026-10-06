from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from ..models import Model, VehicleId

HeaderText = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[\x20-\x7e]+$")]


class Profile(Model):
    """All app identifiers come from operator configuration, never upstream secret defaults."""

    client_id: HeaderText
    redirect_uri: HeaderText
    login_audience: HeaderText
    login_scope: HeaderText
    vehicles_audience: HeaderText
    vss_audience: HeaderText
    mesh_audience: HeaderText
    vat_audience: HeaderText
    login_app_version: HeaderText
    sdk_version: HeaderText
    sign_app_version: HeaderText
    login_user_agent: HeaderText
    api_user_agent: HeaderText

    @field_validator("redirect_uri")
    @classmethod
    def redirect_target(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("invalid_redirect_target")
        return value


class AccountSecrets(Model):
    account_id: VehicleId
    phone: SecretStr
    password: SecretStr
    device_id: SecretStr
    key_id: SecretStr
    hac_key_hex: SecretStr
    app_token: SecretStr

    @model_validator(mode="after")
    def valid_secrets(self) -> AccountSecrets:
        import re

        if not re.fullmatch(r"\+?[0-9]{6,20}", self.phone.get_secret_value()):
            raise ValueError("invalid_phone_format")
        if not 1 <= len(self.password.get_secret_value().encode()) <= 72:
            raise ValueError("unsupported_password_length")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", self.hac_key_hex.get_secret_value()):
            raise ValueError("signing_key_requires_32_bytes_hex")
        for value in (self.device_id, self.key_id, self.app_token):
            if not re.fullmatch(r"[\x21-\x7e]{1,512}", value.get_secret_value()):
                raise ValueError("invalid_secret_header")
        return self


class VehicleBinding(Model):
    vehicle_id: VehicleId
    account_id: VehicleId
    vin: SecretStr
    label: str = Field(min_length=1, max_length=80)
    model_label: str = Field(min_length=1, max_length=64)
    # Must match the cloud modelId; no inference from an L6/L7 display name.
    model_id: str = Field(min_length=1, max_length=80)
    climate_supported: bool = False
    location_supported: bool = False

    @field_validator("vin")
    @classmethod
    def valid_vin(cls, value: SecretStr) -> SecretStr:
        import re

        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.get_secret_value()):
            raise ValueError("invalid_vin_format")
        return value


class CloudConfig(Model):
    profile: Profile
    accounts: list[AccountSecrets] = Field(min_length=1, max_length=20)
    vehicles: list[VehicleBinding] = Field(min_length=1, max_length=100)
    # Separate operator switch; MCP arguments and gateway tokens cannot change it.
    allow_real_control: bool = False

    @model_validator(mode="after")
    def unique_bindings(self) -> CloudConfig:
        account_ids = [a.account_id for a in self.accounts]
        ids = [v.vehicle_id for v in self.vehicles]
        vins = [v.vin.get_secret_value() for v in self.vehicles]
        if len(set(account_ids)) != len(account_ids) or len(set(ids)) != len(ids):
            raise ValueError("duplicate_account_or_vehicle")
        # One real car must have one serialization key, even if shared across vehicle accounts.
        if len(set(vins)) != len(vins):
            raise ValueError("duplicate_vehicle_identity")
        if any(v.account_id not in account_ids for v in self.vehicles):
            raise ValueError("vehicle_account_missing")
        return self


def load_cloud_config(path: Path) -> CloudConfig:
    """Read only an explicitly selected private file; no env discovery or existing HA stores."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("vehicle_secrets_require_private_regular_file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(131073)
        if len(data) > 131072:
            raise ValueError("vehicle_secrets_file_too_large")
        return CloudConfig.model_validate_json(data)
    finally:
        os.close(fd)
