#!/usr/bin/env python3
"""Conservative repository hygiene guard. Complements, does not replace, gitleaks/review."""

import re
import subprocess
from pathlib import Path

files = subprocess.check_output(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard"], text=True
).splitlines()
failures = []
for name in files:
    path = Path(name)
    if not path.is_file():
        continue
    if name.startswith(("runtime/", "secrets/", "custom_components/")) or name.endswith(
        (".sqlite", ".pem", ".key", ".local.json")
    ):
        failures.append((name, "forbidden path"))
    text = path.read_text(errors="replace")
    patterns = {
        "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "JWT": r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}",
        "VIN-shaped literal": r"\b[A-HJ-NPR-Z0-9]{17}\b",
        "China mobile literal": r"\b1[3-9]\d{9}\b",
        "Home Assistant import": r"(?:from|import) homeassistant\b",
    }
    for category, pattern in patterns.items():
        if re.search(pattern, text):
            failures.append((name, category))
if failures:
    for name, category in failures:
        print(f"FAIL {name}: {category}")  # never print matched sensitive values
    raise SystemExit(1)
print(f"PASS: checked {len(files)} public files for forbidden paths and sensitive patterns")
