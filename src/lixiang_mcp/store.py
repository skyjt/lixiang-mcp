from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from .models import ClimateCommand, Operation, OperationEvent, Phase, ServiceError, now

ACTIVE = (Phase.SUBMITTED, Phase.RUNNING, Phase.CLOUD_COMPLETED)


class OperationStore:
    """Single-process SQLite journal. The file lock rejects a second worker/replica.

    Idempotency records are retained indefinitely; there is no automatic command replay.
    SQLite calls contain no network I/O and run atomically between event-loop yields.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise RuntimeError("operation_store_already_in_use") from None
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, key_hash TEXT NOT NULL,
            fingerprint TEXT NOT NULL, vehicle TEXT NOT NULL, body TEXT NOT NULL,
            UNIQUE(subject, key_hash))""")
        self.db.commit()
        for (raw,) in self.db.execute("SELECT body FROM operations").fetchall():
            op = Operation.model_validate_json(raw)
            if op.phase in ACTIVE:
                self.transition(op.operation_id, Phase.UNKNOWN, "interrupted_no_replay")

    def bind_backend(self, namespace: str) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS backend (namespace TEXT NOT NULL)")
        row = self.db.execute("SELECT namespace FROM backend").fetchone()
        if row is not None and row[0] != namespace:
            raise RuntimeError("operation_store_backend_mismatch")
        if row is None:
            if (
                namespace != "mock"
                and self.db.execute("SELECT 1 FROM operations LIMIT 1").fetchone()
            ):
                raise RuntimeError("unbound_store_contains_operations")
            with self.db:
                self.db.execute("INSERT INTO backend VALUES (?)", (namespace,))

    def existing(self, subject: str, command: ClimateCommand) -> Operation | None:
        row = self.db.execute(
            "SELECT fingerprint, body FROM operations WHERE subject=? AND key_hash=?",
            (subject, self.key_hash(command)),
        ).fetchone()
        if row:
            if row[0] != self.fingerprint(command):
                raise ServiceError("idempotency_conflict")
            return Operation.model_validate_json(row[1])
        return None

    @staticmethod
    def key_hash(command: ClimateCommand) -> str:
        return hashlib.sha256(command.idempotency_key.encode()).hexdigest()

    @staticmethod
    def fingerprint(command: ClimateCommand) -> str:
        body = command.model_dump(exclude={"idempotency_key"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def unresolved(self, vehicle_id: str) -> bool:
        rows = self.db.execute("SELECT body FROM operations WHERE vehicle=?", (vehicle_id,))
        return any(Operation.model_validate_json(raw).phase == Phase.UNKNOWN for (raw,) in rows)

    def create(self, subject: str, command: ClimateCommand, *, simulated: bool = True) -> Operation:
        timestamp = now()
        op = Operation(
            operation_id=str(uuid.uuid4()),
            simulated=simulated,
            vehicle_id=command.vehicle_id,
            phase=Phase.SUBMITTED,
            created_at=timestamp,
            updated_at=timestamp,
            events=[OperationEvent(phase=Phase.SUBMITTED, at=timestamp)],
        )
        with self.db:
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    op.operation_id,
                    subject,
                    self.key_hash(command),
                    self.fingerprint(command),
                    command.vehicle_id,
                    op.model_dump_json(),
                ),
            )
        return op

    def get(self, operation_id: str, subject: str | None = None) -> Operation:
        row = self.db.execute(
            "SELECT subject, body FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None or (subject is not None and row[0] != subject):
            raise ServiceError("operation_not_found")
        return Operation.model_validate_json(row[1])

    def transition(self, operation_id: str, phase: Phase, error: str | None = None) -> Operation:
        old = self.get(operation_id)
        timestamp = now()
        op = old.model_copy(
            update={
                "phase": phase,
                "updated_at": timestamp,
                "error_code": error,
                "events": [*old.events, OperationEvent(phase=phase, at=timestamp)],
            }
        )
        with self.db:
            self.db.execute(
                "UPDATE operations SET body=? WHERE id=?", (op.model_dump_json(), operation_id)
            )
        return op

    def close(self) -> None:
        self.db.close()
        self._lock.close()
