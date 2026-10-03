import json
import os
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon


@pytest.fixture
def status_env(home, monkeypatch):
    monkeypatch.setattr(cmon, "STATUS_FAIL", str(home / ".cmon" / "status.fail"))
    return home


def _rows(reset_in):
    return [("session", "Current session", 42.0, (datetime.now(UTC) + reset_in).isoformat(), True)]


def test_cache_write_is_atomic_and_private(status_env):
    cmon._write_cache(_rows(timedelta(hours=1)), datetime.now(UTC))
    d = os.path.dirname(cmon.CACHE)
    assert os.listdir(d) == ["status.json"]  # no temp files left behind
    assert os.stat(cmon.CACHE).st_mode & 0o077 == 0
    assert cmon._read_cache()[0][0][2] == 42.0


def test_expired_cache_offline_fetches_once_then_backs_off(status_env, monkeypatch, capsys):
    cmon._write_cache(_rows(timedelta(minutes=-5)), datetime.now(UTC) - timedelta(hours=1))
    calls = []

    def offline(retries, timeout):
        calls.append(timeout)
        raise cmon.FetchError("network down")

    monkeypatch.setattr(cmon, "fetch", offline)
    for _ in range(3):
        cmon.status(Namespace(live=False, json=True))
    assert calls == [cmon.STATUS_TIMEOUT_S]  # one short attempt, then the cache is served
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(not o["offline"] and o["age_s"] >= 3600 for o in out)


def test_status_survives_locked_db_without_cache(status_env, monkeypatch, capsys):
    monkeypatch.setattr(cmon, "_read_db", lambda: None)
    monkeypatch.setattr(cmon, "fetch", lambda **k: (_ for _ in ()).throw(cmon.FetchError("down")))
    cmon.status(Namespace(live=False, json=True))
    assert json.loads(capsys.readouterr().out)["offline"] is True
