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


@pytest.fixture(autouse=True)
def isolated_loss_rules(monkeypatch, tmp_path):
    """Prevent tests from reading or overwriting the live loss_rules.json at repo root."""
    test_rules_path = tmp_path / "nonexistent_loss_rules.json"
    monkeypatch.setattr("andy_trader.loss_rules.DEFAULT_RULES_PATH", test_rules_path)
    monkeypatch.setattr("run_loss_review.DEFAULT_RULES_PATH", test_rules_path)

