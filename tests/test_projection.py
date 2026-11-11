import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon

T0 = datetime(2030, 1, 2, 6, 0, tzinfo=UTC)  # the clock fixture's instant
H = timedelta(hours=1)
SESSION_RESET = T0 + 4 * H
WEEK_RESET = T0 + 50 * H


def snap(con, at, pct, key, reset):
    role, minutes = (cmon.WindowRole.FIVE_HOUR.value, 300) if key == "session" else ("weekly", 10080)
    con.execute(f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", at, key, role, key, pct, minutes, reset, True, None, None])


@pytest.fixture
def con(home, clock):
    c = cmon.db()
    yield c
    c.close()


@pytest.fixture
def paced(con):
    """5h window rising 20 %/h (hits 100% before its reset), week rising 1 %/h (doesn't)."""
    for mins, pct in ((60, 30), (30, 40), (0, 50)):
        snap(con, T0 - timedelta(minutes=mins), pct, "session", SESSION_RESET)
    for hours, pct in ((10, 20), (5, 25), (0, 30)):
        snap(con, T0 - hours * H, pct, "weekly_all", WEEK_RESET)
    return [("session", "Current session", 50.0, SESSION_RESET.isoformat(), True),
            ("weekly_all", "All models", 30.0, WEEK_RESET.isoformat(), True)]


def at(iso, expected):
    return abs((datetime.fromisoformat(iso) - expected).total_seconds()) < 1


def windows(rows):
    return {w["key"]: w for w in cmon._status_json((rows, 0.0))["providers"]["claude"]["windows"]}


def test_projection_reaches_status_json(con, paced):
    cmon._refresh_projection(con, paced, T0)
    got = windows(paced)
    assert got["session"]["projected_percent"] == pytest.approx(130.0)
    assert at(got["session"]["exhausts_at"], T0 + 2.5 * H)
    assert got["weekly_all"]["projected_percent"] == pytest.approx(80.0)
    assert got["weekly_all"]["exhausts_at"] is None  # 80% at reset: never runs out
    assert {w["projection_at"] for w in got.values()} == {T0.isoformat()}


def test_collect_writes_the_projection_status_reads(con, paced, monkeypatch, capsys):
    con.execute("DELETE FROM snapshots WHERE observed_at = ?", [T0])  # collect stores T0 itself
    con.close()
    monkeypatch.setattr(cmon, "fetch", lambda: {"limits": [
        {"kind": "session", "percent": 50, "resets_at": SESSION_RESET.isoformat(), "is_active": True},
        {"kind": "weekly_all", "percent": 30, "resets_at": WEEK_RESET.isoformat(), "is_active": True}]})
    cmon.collect(Namespace(force=True, alert=False))
    capsys.readouterr()
    cmon.status(Namespace(live=False, json=True))
    got = {w["key"]: w for w in json.loads(capsys.readouterr().out)["providers"]["claude"]["windows"]}
    assert got["session"]["projected_percent"] == pytest.approx(130.0)
    assert at(got["session"]["exhausts_at"], T0 + 2.5 * H)
    assert got["weekly_all"]["projected_percent"] == pytest.approx(80.0)
    assert got["session"]["projection_at"] == T0.isoformat()


def test_now_refreshes_the_projection(con, paced, monkeypatch, capsys):
    con.execute("DELETE FROM snapshots WHERE observed_at = ?", [T0])
    con.close()
    monkeypatch.setattr(cmon, "fetch", lambda: {"limits": [
        {"kind": "session", "percent": 50, "resets_at": SESSION_RESET.isoformat(), "is_active": True}]})
    cmon.now(Namespace(advice=False))
    assert "projection at reset" in capsys.readouterr().out
    assert windows(paced[:1])["session"]["projected_percent"] == pytest.approx(130.0)


def test_projection_of_another_cycle_is_left_out(con, paced):
    cmon._refresh_projection(con, paced, T0)
    next_week = [("weekly_all", "All models", 1.0, (WEEK_RESET + 7 * 24 * H).isoformat(), True)]
    next_5h = [("session", "Current session", 2.0, (SESSION_RESET + 5 * H).isoformat(), True)]
    assert "projected_percent" not in windows(next_week)["weekly_all"]
    assert "exhausts_at" not in windows(next_5h)["session"]


def test_window_at_100_drops_the_projection_cached_before_it(con, paced):
    # A 'status --live' read (cmon Bar) between two collects sees 100% in the same cycle: the
    # cached 130% / exhausts_at from the last collect no longer applies.
    cmon._refresh_projection(con, paced, T0)
    full = [("session", "Current session", 100.0, SESSION_RESET.isoformat(), True), paced[1]]
    got = windows(full)
    assert set(got["session"]) == {"key", "label", "percent", "resets_at", "active"}
    assert got["weekly_all"]["projected_percent"] == pytest.approx(80.0)


def test_projection_the_live_reading_already_passed_is_dropped(con, paced):
    # Usage can't fall inside a cycle: 'Projected 80% at reset' under 85% used is contradicted by
    # the reading next to it, as a cached projection is at 100%. Equal is still a valid pace.
    cmon._refresh_projection(con, paced, T0)
    passed = [paced[0], ("weekly_all", "All models", 85.0, WEEK_RESET.isoformat(), True)]
    got = windows(passed)
    assert set(got["weekly_all"]) == {"key", "label", "percent", "resets_at", "active"}
    assert got["session"]["projected_percent"] == pytest.approx(130.0)
    level = [paced[0], ("weekly_all", "All models", 80.0, WEEK_RESET.isoformat(), True)]
    assert windows(level)["weekly_all"]["projected_percent"] == pytest.approx(80.0)


def test_nothing_to_project_leaves_the_window_without_fields(con):
    reset = (T0 + 3 * H).isoformat()
    snap(con, T0 - H, 100, "session", reset)
    snap(con, T0, 100, "session", reset)              # already full
    snap(con, T0, 40, "weekly_all", reset)            # one reading: no rate yet
    snap(con, T0 - H, 9, "weekly_scoped:Fable", reset)
    snap(con, T0, 9, "weekly_scoped:Fable", reset)    # flat: no rise to project
    rows = [("session", "s", 100.0, reset, True), ("weekly_all", "w", 40.0, reset, True),
            ("weekly_scoped:Fable", "f", 9.0, reset, True), ("other", "o", 5.0, None, False),
            ("late", "l", 5.0, (T0 - H).isoformat(), False)]  # reset already passed
    cmon._refresh_projection(con, rows, T0)
    assert json.load(open(cmon._projection_cache()))["windows"] == {}
    assert all(set(w) == {"key", "label", "percent", "resets_at", "active"} for w in windows(rows).values())


def test_projection_is_capped(con):
    reset = (T0 + 4 * H).isoformat()
    snap(con, T0 - timedelta(minutes=6), 0, "session", reset)
    snap(con, T0, 50, "session", reset)  # 500 %/h
    rows = [("session", "s", 50.0, reset, True)]
    cmon._refresh_projection(con, rows, T0)
    got = windows(rows)["session"]
    assert got["projected_percent"] == cmon.PROJ_CAP
    assert at(got["exhausts_at"], T0 + 0.1 * H)


@pytest.mark.parametrize("bad", [
    "not json",
    '{"computed_at": "2030-01-02T06:00:00+00:00", "windows": []}',
    '{"computed_at": "2030-01-02T06:00:00+00:00", "windows": {"session": '
    '{"resets_at": "2030-01-02T10:00:00+00:00", "projected_percent": "lots", "exhausts_at": null}}}',
    '{"computed_at": "2030-01-02T06:00:00+00:00", "windows": {"session": '
    '{"resets_at": "2030-01-02T10:00:00+00:00", "projected_percent": NaN, "exhausts_at": null}}}',
    '{"computed_at": "2030-01-02T06:00:00+00:00", "windows": {"session": '
    '{"resets_at": "2030-01-02T10:00:00+00:00", "projected_percent": true, "exhausts_at": null}}}',
    '{"computed_at": "2030-01-02T06:00:00+00:00", "windows": {"session": '
    '{"resets_at": "2030-01-02T10:00:00+00:00", "projected_percent": 120, "exhausts_at": 7}}}',
    '{"computed_at": null, "windows": {"session": '
    '{"resets_at": "2030-01-02T10:00:00+00:00", "projected_percent": 120, "exhausts_at": null}}}',
])
def test_a_broken_cache_is_ignored(home, clock, bad):
    path = cmon._projection_cache()
    cmon._private_dir(cmon.CMON_HOME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(bad)
    rows = [("session", "s", 50.0, SESSION_RESET.isoformat(), True)]
    assert set(windows(rows)["session"]) == {"key", "label", "percent", "resets_at", "active"}


def test_a_failed_refresh_never_fails_its_caller(con, paced, monkeypatch, capsys):
    def boom(*_a):
        raise RuntimeError("db went away")

    monkeypatch.setattr(cmon, "_projections", boom)
    cmon._refresh_projection(con, paced, T0)
    assert "projection skipped: db went away" in capsys.readouterr().err
    cmon._refresh_projection(con, paced, T0, quiet=True)
    assert capsys.readouterr().err == ""
    assert "projected_percent" not in windows(paced)["session"]


def test_the_cache_is_private(con, paced):
    import os
    cmon._refresh_projection(con, paced, T0)
    assert os.stat(cmon._projection_cache()).st_mode & 0o077 == 0
