"""Private, locked, atomically replaced local files; encrypted wizard checkpoint."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from nacl.secret import SecretBox
from pydantic import BaseModel

from ..private_files import private_directory, private_read, private_write, secret_json


class SetupStore:
    def __init__(self, directory: Path) -> None:
        self._closed = False
        private_directory(directory)
        self.directory = directory.absolute()
        self._lock = os.open(
            directory / "setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            key_path, state_path = directory / "key.bin", directory / "state.box"
            if not key_path.exists():
                if state_path.exists():
                    raise ValueError("setup_key_missing")
                private_write(key_path, os.urandom(SecretBox.KEY_SIZE))
            self._box = SecretBox(private_read(key_path, SecretBox.KEY_SIZE))
            self.path = state_path
        except Exception:
            os.close(self._lock)
            raise

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            data = json.loads(self._box.decrypt(private_read(self.path)))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except Exception:
            raise ValueError("setup_checkpoint_invalid") from None

    def write(self, state: BaseModel) -> None:
        private_write(self.path, bytes(self._box.encrypt(secret_json(state))))

    def close(self) -> None:
        if not self._closed:
            os.close(self._lock)
            self._closed = True
