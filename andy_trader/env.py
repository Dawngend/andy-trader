"""Repo paths and a dependency-free .env loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

# andy_trader/env.py -> andy_trader/ -> repo root. Config and the database live
# at the root, not inside the package, so this deliberately climbs two levels.
REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigurationError(RuntimeError):
    """Raised when a .env file cannot be parsed."""


def load_env_file(path: Path, environ: MutableMapping[str, str] | None = None) -> None:
    """Load simple KEY=VALUE settings without overwriting the process environment.

    Uses `setdefault`, so anything already exported wins over the file. That
    ordering matters: it lets a scheduled run or a CI job override a checked-in
    default without editing the file.

    Vendored from omni-router's `omni_cli.py` rather than imported, so this
    project stands alone as its own repository with no dependency on the vault.
    """

    target: MutableMapping[str, str] = os.environ if environ is None else environ
    if not path.is_file():
        return
    # utf-8-sig because Windows editors routinely leave a BOM on .env files, and
    # a BOM silently corrupts the first key name into something unmatchable.
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigurationError(f"Invalid .env line {line_number}: empty key")
        target.setdefault(key, value.strip().strip("\"'"))
