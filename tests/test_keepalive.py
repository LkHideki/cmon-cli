import os
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import cmon


def _rows(reset):
    return [("session", "Current session", 0.0, reset, False)]


def _iso(delta):
    return (datetime.now(UTC) + delta).isoformat()


PARKED = _rows(None)


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(cmon.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    return calls


@pytest.fixture
def enabled():
    cmon._write_config({"keepalive": "on"})


@pytest.mark.parametrize(("rows", "parked"), [
    (PARKED, True),
    (_rows(_iso(timedelta(minutes=-1))), True),
    (_rows(_iso(timedelta(hours=4))), False),
    ([("weekly_all", "All models", 5.0, None, True)], False),  # no session row: nothing to open
    ([], False),
])
def test_window_parked(rows, parked):
    assert cmon._window_parked(rows) is parked


def test_off_by_default(spawned):
    cmon._keepalive_check(PARKED)
    assert spawned == []


def test_running_window_is_left_alone(spawned, enabled):
    cmon._keepalive_check(_rows(_iso(timedelta(hours=4))))
    assert spawned == []


def test_parked_window_spawns_one_detached_ping_per_cooldown(spawned, enabled):
    for _ in range(3):  # the menu bar polls every 5 s right after a rollover
        cmon._keepalive_check(PARKED)
    assert len(spawned) == 1
    cmd, kw = spawned[0]
    assert cmd[-1] == "keepalive" and kw["start_new_session"] is True

    old = datetime.now(UTC).timestamp() - cmon.KEEPALIVE_RETRY_S - 1
    os.utime(cmon.KEEPALIVE_STAMP, (old, old))
    cmon._keepalive_check(PARKED)
    assert len(spawned) == 2


def test_live_status_triggers_ping(spawned, enabled, monkeypatch):
    monkeypatch.setattr(cmon, "fetch", lambda **kw: {"limits": [
        {"kind": "session", "percent": 0, "resets_at": None, "is_active": False}]})
    cmon.status(Namespace(live=True, json=True))
    assert len(spawned) == 1


def test_collect_triggers_ping(spawned, enabled, monkeypatch):
    monkeypatch.setattr(cmon, "fetch", lambda: {"limits": [
        {"kind": "session", "percent": 0, "resets_at": None, "is_active": False}]})
    cmon.collect(Namespace(force=True, alert=False))
    assert len(spawned) == 1


def test_failed_fetch_with_an_expired_cache_still_pings(spawned, enabled, monkeypatch):
    # An idle Claude Code's token expires, so the fetch fails exactly when the window is parked.
    cmon._write_cache(_rows(_iso(timedelta(minutes=-5))), datetime.now(UTC) - timedelta(hours=6))

    def expired(**_kw):
        raise cmon.FetchError("token expired")

    monkeypatch.setattr(cmon, "fetch", expired)
    with pytest.raises(cmon.FetchError):
        cmon.collect(Namespace(force=True, alert=False))
    assert len(spawned) == 1


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout="0.33\n", stderr="")

    monkeypatch.setattr(cmon.subprocess, "run", run)
    monkeypatch.setattr(cmon, "_claude_bin", lambda: "/bin/claude")
    return calls


def test_ping_uses_haiku_a_new_number_and_the_plan(ran, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cmon.keepalive(None)
    cmon.keepalive(None)
    (first, kw), (second, _) = ran
    assert first[first.index("--model") + 1] == "haiku"
    assert first[first.index("-p") + 1] != second[second.index("-p") + 1]
    assert "ANTHROPIC_API_KEY" not in kw["env"]  # would bill the key and never open the window
    assert "keepalive: cos(" in capsys.readouterr().out


def test_ping_reports_failure(ran, monkeypatch):
    monkeypatch.setattr(cmon.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="not logged in"))
    with pytest.raises(SystemExit, match="not logged in"):
        cmon.keepalive(None)


def test_ping_without_claude_says_so(monkeypatch):
    monkeypatch.setattr(cmon, "_claude_bin", lambda: None)
    with pytest.raises(SystemExit, match="claude command"):
        cmon.keepalive(None)


def test_config_validates_value():
    assert cmon._validate_keepalive("on") == "on"
    with pytest.raises(ValueError, match="keepalive"):
        cmon._validate_keepalive("yes")
