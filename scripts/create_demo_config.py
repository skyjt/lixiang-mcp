#!/usr/bin/env python3
"""Generate a local mock credential. Never prints the credential or overwrites existing files."""

import hashlib
import json
import os
import secrets
from pathlib import Path

os.umask(0o077)
runtime = Path("runtime")
runtime.mkdir(exist_ok=True)
paths = [runtime / "auth.json", runtime / "demo-token", Path("config.local.json")]
if any(p.exists() for p in paths):
    raise SystemExit(
        "Local config already exists; preserve it or move it before generating another."
    )
token = secrets.token_urlsafe(32)
auth = {
    "credentials": [
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": {
                "subject": "demo-user",
                "account_id": "demo-account",
                "vehicle_ids": ["demo-l6", "demo-l7", "demo-unknown"],
                "scopes": ["vehicle:read"],
            },
        }
    ]
}
paths[0].write_text(json.dumps(auth, indent=2) + "\n")
paths[1].write_text(token + "\n")
paths[2].write_text(
    json.dumps(
        {
            "backend": "mock",
            "auth_file": "runtime/auth.json",
            "database": "runtime/operations.sqlite",
            "enable_control": False,
            "host": "127.0.0.1",
            "port": 8000,
        },
        indent=2,
    )
    + "\n"
)
print("Created runtime/auth.json, runtime/demo-token and config.local.json (read-only mock).")
