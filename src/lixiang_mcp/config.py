from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import Model, Principal


class Credential(Model):
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    principal: Principal


class AuthConfig(Model):
    credentials: list[Credential] = Field(min_length=1, max_length=100)


class Settings(Model):
    backend: Literal["mock"] = "mock"
    auth_file: Path
    database: Path = Path("runtime/operations.sqlite")
    enable_control: bool = False
    command_timeout: float = Field(default=5, ge=0.05, le=60)
    stale_after_seconds: float = Field(default=120, gt=0)
    mock_fault: Literal["none", "timeout", "reject", "unconfirmed", "stale"] = "none"
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]
    allowed_origins: list[str] = []
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    @classmethod
    def from_env(cls) -> Settings:
        # One JSON config file. Secrets never need command-line flags or dotenv contents.
        filename = os.environ.get("LIXIANG_CONFIG", "config.local.json")
        return cls.model_validate_json(Path(filename).read_text())
