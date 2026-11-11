import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
RESET = T0 + timedelta(days=3)  # weekly cycle that began 4 days before T0
MIN = timedelta(minutes=1)
_n = 0


def snap(con, at, pct, reset=RESET, key="weekly_all", window=10080):
    con.execute(f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", at, key, "weekly", key, pct, window, reset, True, None, None])


def tok(con, at, surface, out=1_000_000, model="claude-opus-4-1", project="p"):
    """One message of `out` output tokens: Opus output costs US$ 25 per million."""
    global _n
    _n += 1
    con.execute(f"INSERT INTO token_log({cmon._TOKEN_LOG_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", f"m{_n}:r", at, model, surface, project, "s", 0, out, 0, 0, 0, 0, None])


@pytest.fixture
def con(home):
    return cmon.db()


def share(con, **kw):
    got = cmon._plan_share(con, kw.pop("key", "weekly_all"), **kw)
    return {g: round(v, 6) for g, v in got["groups"].items()}, round(got["other"], 6), got


def test_rise_split_by_cost_in_the_interval(con):
    snap(con, T0, 10)
    tok(con, T0 + MIN, "cli", out=3_000_000)
    tok(con, T0 + 2 * MIN, "claude-vscode", out=1_000_000)
    snap(con, T0 + 10 * MIN, 14)
    groups, other, got = share(con, since=T0)
    assert groups == {"cli": 3.0, "claude-vscode": 1.0}
    assert other == 0
    assert got["total"] == pytest.approx(4)


def test_cost_not_raw_tokens_decides(con):
    snap(con, T0, 0)
    tok(con, T0 + MIN, "cli", out=1_000_000, model="claude-opus-4-1")       # US$ 25
    tok(con, T0 + MIN, "claude-vscode", out=5_000_000, model="claude-haiku")  # US$ 25
    snap(con, T0 + 10 * MIN, 2)
    groups, _other, _ = share(con, since=T0)
    assert groups == {"cli": 1.0, "claude-vscode": 1.0}


def test_rise_without_local_cost_is_other(con):
    snap(con, T0, 10)
    snap(con, T0 + 10 * MIN, 13)  # claude.ai chat, another machine…
    groups, other, _ = share(con, since=T0)
    assert groups == {} and other == 3


def test_flat_intervals_carry_cost_to_the_next_tick(con):
    snap(con, T0, 10)
    tok(con, T0 + MIN, "cli")
    snap(con, T0 + 10 * MIN, 10)  # integer percent hasn't moved yet
    tok(con, T0 + 11 * MIN, "claude-vscode")
    snap(con, T0 + 20 * MIN, 12)
    groups, other, _ = share(con, since=T0)
    assert groups == {"cli": 1.0, "claude-vscode": 1.0} and other == 0


def test_new_cycle_counts_first_reading_since_cycle_start(con):
    old_reset, start = T0, T0  # previous cycle ended at T0; next one runs to T0 + 7d
    snap(con, T0 - 10 * MIN, 90, reset=old_reset)
    tok(con, T0 - 5 * MIN, "claude-vscode")  # previous cycle: not this one's
    tok(con, start + MIN, "cli")
    snap(con, start + 30 * MIN, 5, reset=T0 + timedelta(days=7))
    groups, other, got = share(con, since=start)
    assert groups == {"cli": 5.0} and other == 0 and got["cycles"] == 1


def test_reset_jitter_is_the_same_cycle(con):
    snap(con, T0, 10)
    tok(con, T0 + MIN, "cli")
    snap(con, T0 + 10 * MIN, 12, reset=RESET + timedelta(milliseconds=300))
    groups, _other, got = share(con, since=T0)
    assert groups == {"cli": 2.0} and got["cycles"] == 1


def test_drop_inside_cycle_is_ignored(con):
    snap(con, T0, 10)
    snap(con, T0 + 10 * MIN, 9)
    tok(con, T0 + 11 * MIN, "cli")
    snap(con, T0 + 20 * MIN, 11)
    groups, other, got = share(con, since=T0)
    assert groups == {"cli": 2.0} and other == 0 and got["total"] == pytest.approx(2)


def test_scoped_window_counts_only_its_model(con):
    snap(con, T0, 0, key="weekly_scoped:Fable")
    tok(con, T0 + MIN, "cli", model="claude-opus-4-1")
    tok(con, T0 + MIN, "claude-vscode", model="claude-fable-5")
    snap(con, T0 + 10 * MIN, 4, key="weekly_scoped:Fable")
    groups, _other, _ = share(con, key="weekly_scoped:Fable", since=T0)
    assert groups == {"claude-vscode": 4.0}


def test_group_column_and_filter(con):
    snap(con, T0, 0)
    tok(con, T0 + MIN, "cli", project="a")
    tok(con, T0 + MIN, "claude-vscode", project="b", out=3_000_000)
    snap(con, T0 + 10 * MIN, 4)
    groups, _other, _ = share(con, since=T0, col="project")
    assert groups == {"a": 1.0, "b": 3.0}
    groups, _other, got = share(con, since=T0, col="project", where=" AND surface = ?", params=["cli"])
    assert groups == {"a": 1.0} and got["rest"] == pytest.approx(3)  # filtering doesn't inflate what's left


def test_no_snapshots_is_none(con):
    tok(con, T0, "cli")
    assert cmon._plan_share(con, "weekly_all", since=T0) is None


def burn(capsys, **kw):
    args = {"by": "surface", "since": None, "json": True, "all_accounts": False, "plan": "weekly", **kw}
    cmon.burn(Namespace(**args))
    out = capsys.readouterr().out
    return json.loads(out) if args["json"] else out


@pytest.fixture
def week(con):
    """Cycle T0-4d → RESET; one terminal message before the cycle began, two inside it."""
    tok(con, RESET - timedelta(days=8), "cli")  # previous cycle
    snap(con, T0, 10)
    tok(con, T0 + MIN, "cli", out=3_000_000)
    tok(con, T0 + MIN, "claude-vscode")
    snap(con, T0 + 10 * MIN, 14)
    snap(con, T0 + 20 * MIN, 16)  # rose with nothing logged
    con.close()


def test_burn_plan_defaults_to_current_cycle(week, capsys):
    got = burn(capsys)
    assert got["window"] == "weekly_all" and got["cycles"] == 1
    assert got["since"].startswith((RESET - timedelta(days=7)).date().isoformat())
    by = {g["grp"]: g for g in got["groups"]}
    assert by["cli"]["tok"] == 3_000_000  # the message of the previous cycle is outside
    # the first reading (10%) has no cost logged since the cycle began, so it is 'other' too
    assert by["cli"]["plan_pp"] == pytest.approx(3) and by["claude-vscode"]["plan_pp"] == pytest.approx(1)
    assert got["other_pp"] == pytest.approx(12) and got["total_pp"] == pytest.approx(16)


def test_burn_plan_text(week, capsys):
    out = burn(capsys, json=False)
    assert "terminal" in out and "3.0 pp" in out and "other (no local log)" in out
    assert "16.0 pp  100%" in out and "this cycle" in out


def test_burn_plan_aliases_and_unknown_window(week, capsys):
    assert burn(capsys, plan="weekly_all")["window"] == "weekly_all"
    with pytest.raises(SystemExit, match="Known: weekly_all"):
        burn(capsys, plan="5h")


def test_burn_plan_needs_snapshots(home, capsys):
    with pytest.raises(SystemExit, match="cmon collect"):
        burn(capsys)


def test_burn_surface_filter(week, capsys):
    got = burn(capsys, plan=None, since="all", by="model", surface="vscode")
    assert [g["tok"] for g in got] == [1_000_000]
    got = burn(capsys, by="project", surface="terminal,claude-vscode")
    assert sum(g["tok"] for g in got["groups"]) == 4_000_000 and got["rest_pp"] == 0
    got = burn(capsys, by="project", surface="vscode")
    assert got["groups"][0]["plan_pp"] == pytest.approx(1) and got["rest_pp"] == pytest.approx(3)
    out = burn(capsys, json=False, surface="vscode")
    assert "· vscode only" in out and "rest (filtered out)" in out


def test_client_split_reaches_status_json(con, monkeypatch):
    snap(con, T0, 0)
    tok(con, T0 + MIN, "cli", out=3_000_000)
    tok(con, T0 + MIN, "claude-vscode")
    snap(con, T0 + 10 * MIN, 8)
    rows = [("weekly_all", "All models", 8.0, RESET.isoformat(), True)]
    cmon._refresh_client_share(con, rows, T0 + 10 * MIN)
    w = cmon._status_json((rows, 0.0))["providers"]["claude"]["windows"][0]
    assert w["by_client"]["clients"] == [{"key": "cli", "label": "Terminal", "share": 0.75},
                                         {"key": "claude-vscode", "label": "VS Code", "share": 0.25}]
    assert w["by_client"]["other"] == 0
    # a new cycle began: the cached split belongs to the old one and is left out
    later = [("weekly_all", "All models", 1.0, (RESET + timedelta(days=7)).isoformat(), True)]
    assert "by_client" not in cmon._status_json((later, 0.0))["providers"]["claude"]["windows"][0]


def test_status_json_without_split_is_unchanged(home):
    rows = [("weekly_all", "All models", 8.0, RESET.isoformat(), True)]
    assert set(cmon._status_json((rows, 0.0))["providers"]["claude"]["windows"][0]) == {
        "key", "label", "percent", "resets_at", "active"}
