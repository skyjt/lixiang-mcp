"""Explicit private-file access and atomic writes. Never discovers credentials."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr


def secret_json(value: Any) -> bytes:
    def plain(item: Any) -> Any:
        if isinstance(item, SecretStr):
            return item.get_secret_value()
        if isinstance(item, BaseModel):
            return plain(item.model_dump(mode="python"))
        if isinstance(item, dict):
            return {key: plain(v) for key, v in item.items()}
        if isinstance(item, (list, tuple, set, frozenset)):
            return [plain(v) for v in item]
        return item

    return json.dumps(plain(value), ensure_ascii=False).encode()


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError("setup_directory_must_be_private")


def private_read(path: Path, limit: int = 1_048_576) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("setup_file_must_be_private")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("setup_file_too_large")
        return data
    finally:
        os.close(fd)


def private_write(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        private_read(path)
    temporary = path.with_name(".pending-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
