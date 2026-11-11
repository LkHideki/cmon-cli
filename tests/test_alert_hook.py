import shlex
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import cmon

FIELDS = ("CMON_ALERT_KIND", "CMON_ALERT_KEY", "CMON_ALERT_PERCENT", "CMON_ALERT_RESETS_AT",
          "CMON_ALERT_MSG")


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """A real CMON_HOOK that appends one tab-separated line per run: the alert fields, then
    whether cmon's own credential leaked into its environment. An unset field prints 'unset',
    so a field that is set but empty (the promise to hooks under `set -u`) shows as ''."""
    out = tmp_path / "hook.log"
    fields = " ".join(f'"${{{f}-unset}}"' for f in FIELDS)
    fmt = "%s\\t" * len(FIELDS) + "%s\\n"
    monkeypatch.setenv("CMON_HOOK", f"printf '{fmt}' {fields} \"${{CLAUDE_OAUTH_TOKEN-unset}}\" "
                                    f">> {shlex.quote(str(out))}")
    monkeypatch.setattr(cmon, "_notify", lambda title, msg: None)

    def runs():
        return [line.split("\t") for line in out.read_text().splitlines()] if out.exists() else []

    return runs


def _rate(monkeypatch, alerts):
    monkeypatch.setattr(cmon, "_rate_alerts", lambda rows, con, tz=None: alerts)


def test_rate_alert_runs_hook_once_per_cycle_with_its_fields(hook, monkeypatch):
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "dummy-test-value")
    rows = [("session", "Current session", 87.0, "2030-01-02T05:00:00.250000-03:00", True)]
    alerts = [("session", rows[0][3], "Current session: 100% in ~1.0h")]
    _rate(monkeypatch, alerts)
    for _ in range(3):  # three scheduled collects in the same cycle: the repeats are deduped
        cmon._fire_rate_alerts(rows, None)
    assert hook() == [["rate", "session", "87", "2030-01-02T08:00:00+00:00",
                       "Current session: 100% in ~1.0h", "unset"]]
    alerts[0] = ("session", "2030-01-02T13:00:00+00:00", "again")  # next cycle fires again
    cmon._fire_rate_alerts(rows, None)
    assert [r[3:5] for r in hook()] == [["2030-01-02T08:00:00+00:00", "Current session: 100% in ~1.0h"],
                                        ["2030-01-02T13:00:00+00:00", "again"]]


def test_lead_alert_runs_hook_with_its_fields(hook, clock, monkeypatch):
    monkeypatch.setitem(cmon.LEAD_MIN_BY_KEY, "session", 60)
    monkeypatch.setitem(cmon.LEAD_MIN_BY_KEY, "weekly_all", 180)
    rows = [("session", "Current session", 92.4, "2030-01-02T06:30:00+00:00", True),
            ("weekly_all", "All models", 40.0, "2030-01-05T00:00:00+00:00", True)]  # far off
    cmon._fire_lead_alert(rows)
    cmon._fire_lead_alert(rows)
    assert [r[:4] for r in hook()] == [["lead", "session", "92.4", "2030-01-02T06:30:00+00:00"]]


def test_rate_and_lead_dedup_separately(hook, clock, monkeypatch):
    monkeypatch.setitem(cmon.LEAD_MIN_BY_KEY, "session", 60)
    rows = [("session", "Current session", 95.0, "2030-01-02T06:30:00+00:00", True)]
    _rate(monkeypatch, [("session", rows[0][3], "Current session: 100% in ~0.2h")])
    cmon._fire_rate_alerts(rows, None)
    cmon._fire_lead_alert(rows)
    assert [r[0] for r in hook()] == ["rate", "lead"]


def test_hook_gets_empty_fields_when_unknown_and_never_evaluates_the_message(hook, monkeypatch,
                                                                             tmp_path):
    msg = 'odd "$(touch pwned)" `touch pwned2` $HOME'
    _rate(monkeypatch, [("weekly_all", "2030-01-05T00:00:00+00:00", msg)])
    cmon._fire_rate_alerts([], None)  # no row for the window: percent unknown
    assert hook() == [["rate", "weekly_all", "", "2030-01-05T00:00:00+00:00", msg, "unset"]]
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


def test_without_hook_nothing_is_spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(cmon, "_notify", lambda title, msg: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    _rate(monkeypatch, [("session", "2030-01-02T08:00:00+00:00", "m")])
    cmon._fire_rate_alerts([], None)
    assert calls == []


@pytest.mark.parametrize("failure", ["exit 3", "no-such-command-cmon-test", "timeout"])
def test_broken_hook_never_breaks_the_alert(monkeypatch, failure):
    sent = []
    monkeypatch.setattr(cmon, "_notify", lambda title, msg: sent.append(msg))
    monkeypatch.setenv("CMON_HOOK", failure)
    if failure == "timeout":
        def hang(*_a, **_k):
            raise subprocess.TimeoutExpired("hook", 30)

        monkeypatch.setattr(subprocess, "run", hang)
    _rate(monkeypatch, [("session", "2030-01-02T08:00:00+00:00", "m")])
    cmon._fire_rate_alerts([], None)
    cmon._fire_rate_alerts([], None)
    assert sent == ["m"]  # notified once and marked as fired despite the hook failing


def test_hook_env_formats_percent_and_reset():
    env = cmon._hook_env("m", "lead", "session", 69,
                         datetime(2030, 1, 2, 3, 0, 0, 999, tzinfo=timezone(timedelta(hours=9))))
    assert env == {"CMON_ALERT_MSG": "m", "CMON_ALERT_KIND": "lead", "CMON_ALERT_KEY": "session",
                   "CMON_ALERT_PERCENT": "69", "CMON_ALERT_RESETS_AT": "2030-01-01T18:00:00+00:00"}
    assert cmon._hook_env("m", "rate", "k", 5.5, None)["CMON_ALERT_PERCENT"] == "5.5"
    assert cmon._hook_env("m", "rate", "k", None, "")["CMON_ALERT_RESETS_AT"] == ""
    assert cmon._hook_env("m", "rate", "k") == {  # unknown: still set, empty
        "CMON_ALERT_MSG": "m", "CMON_ALERT_KIND": "rate", "CMON_ALERT_KEY": "k",
        "CMON_ALERT_PERCENT": "", "CMON_ALERT_RESETS_AT": ""}


def test_hook_under_set_u_survives_unknown_fields(monkeypatch, tmp_path):
    out = tmp_path / "hook.log"
    monkeypatch.setenv("CMON_HOOK", f'set -u; printf "[%s][%s]" "$CMON_ALERT_PERCENT" "$CMON_ALERT_RESETS_AT" '
                                    f'> {shlex.quote(str(out))}')
    cmon._run_hook("m", kind="rate", key="weekly_all")
    assert out.read_text() == "[][]"
