"""Opt-in rotation persistence for an explicitly provisioned private configuration file."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from ..private_files import private_directory, private_write, secret_json
from .config import CloudConfig, SavedSession, load_cloud_config
from .transport import ProtocolError


def configuration_digest(config: CloudConfig) -> bytes:
    data = config.model_dump(exclude={"accounts": {"__all__": {"saved_session"}}})
    return hashlib.sha256(secret_json(data)).digest()


class SessionFile:
    def __init__(self, path: Path, config: CloudConfig) -> None:
        private_directory(path.parent)
        self.path, self.expected = path, configuration_digest(config)
        self._lock = asyncio.Lock()

    def _write(self, account_id: str, session: SavedSession) -> None:
        config = load_cloud_config(self.path)
        if configuration_digest(config) != self.expected:
            raise ValueError("configuration_changed_while_running")
        matching = [a for a in config.accounts if a.account_id == account_id]
        if len(matching) != 1 or matching[0].device_id != session.device_id:
            raise ValueError("session_account_mismatch")
        updated = config.model_copy(
            update={
                "accounts": [
                    a.model_copy(update={"saved_session": session})
                    if a.account_id == account_id
                    else a
                    for a in config.accounts
                ]
            }
        )
        private_write(self.path, secret_json(updated))

    async def update(self, account_id: str, session: SavedSession) -> None:
        async with self._lock:
            work = asyncio.create_task(asyncio.to_thread(self._write, account_id, session))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # A thread cannot be cancelled. Hold the writer lock until its atomic write
                # finishes, so shutdown/retry cannot let an older rotation overwrite a newer one.
                await asyncio.gather(work, return_exceptions=True)
                raise
            except Exception:
                raise ProtocolError("private_session_persistence_failed") from None
