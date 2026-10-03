import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cmon  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME, DB and cache: tests never touch the user's real ~/.cmon or ~/.claude."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cmon, "DB", str(tmp_path / ".cmon" / "usage.duckdb"))
    monkeypatch.setattr(cmon, "CACHE", str(tmp_path / ".cmon" / "status.json"))
    monkeypatch.setattr(cmon, "LOGS_ROOT", str(tmp_path / ".claude" / "projects"))
    return tmp_path
