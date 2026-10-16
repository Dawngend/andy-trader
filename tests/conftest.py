"""Put the repository root on sys.path so `andy_trader` imports without install."""

from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def default_starting_cash(monkeypatch):
    """The repo's .env sets the real paper starting balance; tests expect the built-in default."""
    monkeypatch.delenv("CRYPTO_PAPER_STARTING_CASH", raising=False)
