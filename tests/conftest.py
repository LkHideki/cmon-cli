from __future__ import annotations

import contextlib
import io
import json
import shutil
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cmon

FIXTURES = Path(__file__).parent / "fixtures"
GOLDENS = Path(__file__).parent / "golden"


@dataclass(frozen=True)
class IsolatedPaths:
    root: Path
    home: Path
    db: Path
    cache: Path
    config: Path
    logs: Path


@dataclass(frozen=True)
class CliResult:
    exit_code: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, int | str]:
        return {"exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr}


class Clock:
    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("test clock requires an aware datetime")
        self.instant = instant.astimezone(UTC)

    def set(self, value: str | datetime) -> None:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if instant.tzinfo is None:
            raise ValueError("test clock requires an aware datetime")
        self.instant = instant.astimezone(UTC)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IsolatedPaths:
    """Redirect every persistent path and fail fast if a test attempts real network I/O."""
    home = tmp_path / "home"
    paths = IsolatedPaths(
        root=tmp_path,
        home=home,
        db=tmp_path / "usage.duckdb",
        cache=home / ".cmon" / "status.json",
        config=home / ".cmon" / "config.json",
        logs=home / ".claude" / "projects",
    )
    paths.logs.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    for key in (
        "CLAUDE_OAUTH_TOKEN",
        "CMON_DB",
        "CMON_TZ",
        "CMON_QUIET_HOURS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cmon, "DB", str(paths.db))
    monkeypatch.setattr(cmon, "CACHE", str(paths.cache))
    monkeypatch.setattr(cmon, "CONFIG", str(paths.config))
    monkeypatch.setattr(cmon, "LOGS_ROOT", str(paths.logs))
    # Module constants resolved at import time, against the real HOME.
    monkeypatch.setattr(cmon, "CMON_HOME", str(home / ".cmon"))
    monkeypatch.setattr(cmon, "ALERT_STATE", str(home / ".cmon" / "alerts.json"))
    monkeypatch.setattr(cmon, "STATUS_FAIL", str(home / ".cmon" / "status.fail"))
    monkeypatch.setattr(cmon, "LEGACY_PURGED", str(home / ".cmon" / ".legacy-chain-purged"))
    monkeypatch.setattr(cmon, "DESKTOP_SESSIONS", str(home / "desktop-sessions"))

    def blocked(*_args, **_kwargs):
        raise AssertionError("tests must not use the network")

    monkeypatch.setattr(cmon, "_session", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    return paths


@pytest.fixture
def paths(isolated_runtime: IsolatedPaths) -> IsolatedPaths:
    return isolated_runtime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(datetime(2030, 1, 2, 6, 0, tzinfo=UTC))
    real_datetime = datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock.instant
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(cmon, "datetime", FrozenDateTime)
    return clock


@pytest.fixture
def fixture_json() -> Callable[[str], dict]:
    def load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def install_claude_log(paths: IsolatedPaths) -> Callable[[str], Path]:
    def install(name: str = "claude-transcript.jsonl") -> Path:
        target = paths.logs / "synthetic-project" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / "claude-transcript.jsonl", target)
        return target

    return install


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Callable[..., CliResult]:
    """Invoke cmon's real parser/dispatcher without spawning a process."""

    def invoke(*args: str) -> CliResult:
        stdout, stderr = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "argv", ["cmon", *args])
        code = 0
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cmon.main()
            except SystemExit as exc:
                if isinstance(exc.code, str):
                    print(exc.code, file=sys.stderr)
                    code = 1
                else:
                    code = int(exc.code or 0)
        return CliResult(code, stdout.getvalue(), stderr.getvalue())

    return invoke


@pytest.fixture
def golden() -> Callable[[str, CliResult], None]:
    expected = json.loads((GOLDENS / "claude-cli.json").read_text(encoding="utf-8"))

    def compare(name: str, result: CliResult) -> None:
        if name not in expected:
            raise AssertionError(f"missing golden {name!r}: {result.as_dict()!r}")
        assert result.as_dict() == expected[name]

    return compare


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME rooted at tmp_path, with the DB under ~/.cmon as in production."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cmon, "DB", str(tmp_path / ".cmon" / "usage.duckdb"))
    monkeypatch.setattr(cmon, "CACHE", str(tmp_path / ".cmon" / "status.json"))
    monkeypatch.setattr(cmon, "LOGS_ROOT", str(tmp_path / ".claude" / "projects"))
    monkeypatch.setattr(cmon, "CMON_HOME", str(tmp_path / ".cmon"))
    return tmp_path
