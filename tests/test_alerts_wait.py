import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon


@pytest.fixture
def alert_env(home, monkeypatch):
    monkeypatch.setattr(cmon, "ALERT_STATE", str(home / ".cmon" / "alerts.json"))
    sent = []
    monkeypatch.setattr(cmon, "_notify", lambda title, msg: sent.append(msg))
    return sent


def test_rate_alert_notifies_once_per_window_cycle(alert_env, monkeypatch):
    reset = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    alerts = [("session", reset, "Current session: 100% in ~1.0h")]
    monkeypatch.setattr(cmon, "_rate_alerts", lambda rows, con, tz=None: alerts)
    for _ in range(3):  # three scheduled collects in the same cycle
        cmon._fire_rate_alerts([], None)
    assert alert_env == ["Current session: 100% in ~1.0h"]
    # next cycle (new reset) alerts again
    alerts[0] = ("session", (datetime.now(UTC) + timedelta(hours=7)).isoformat(), "again")
    cmon._fire_rate_alerts([], None)
    assert alert_env[-1] == "again" and len(alert_env) == 2


def test_wait_rides_out_transient_failures(home, monkeypatch, capsys):
    monkeypatch.setattr(cmon, "_notify", lambda *a: None)
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    script = [cmon.FetchError("network down"), cmon.FetchError("network down"),
              [("session", "Current session", 50.0, None, True)],
              [("session", "Current session", 91.0, None, True)]]

    def snapshot():
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step, datetime.now(UTC)

    monkeypatch.setattr(cmon, "_snapshot", snapshot)
    cmon.wait(Namespace(window="session", at=90, interval=10))
    err = capsys.readouterr().err
    assert err.count("network down") == 1 and "connection restored" in err
    assert sleeps[:2] == [10, 20]  # exponential backoff while offline


def test_wait_stops_on_permanent_failure(home, monkeypatch):
    def snapshot():
        raise cmon.FetchError("no token", transient=False)

    monkeypatch.setattr(cmon, "_snapshot", snapshot)
    with pytest.raises(cmon.FetchError):
        cmon.wait(Namespace(window="session", at=90, interval=10))


def test_alert_state_is_written_atomically(alert_env):
    cmon._mark_alert("rate_session", 123)
    assert json.load(open(cmon.ALERT_STATE)) == {"rate_session": 123}
    assert cmon.os.listdir(cmon.os.path.dirname(cmon.ALERT_STATE)) == ["alerts.json"]
