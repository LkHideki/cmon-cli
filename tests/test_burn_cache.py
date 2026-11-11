import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
_n = 0


def turn(con, at, session="s1", project="p", model="claude-opus-4-1", surface="cli", i=0, o=0, r=0, w=0):
    """One API message. Opus: input US$ 5, output 25, cache read 0.50, cache write 10 per million."""
    global _n
    _n += 1
    con.execute(f"INSERT INTO token_log({cmon._TOKEN_LOG_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", f"m{_n}:r", at, model, surface, project, session, i, o, 0, r, w, 0, None])


@pytest.fixture
def con(home):
    return cmon.db()


def burn(capsys, **kw):
    args = {"by": None, "since": "all", "json": True, "all_accounts": False, "plan": None, "cache": True, **kw}
    cmon.burn(Namespace(**args))
    out = capsys.readouterr().out
    return json.loads(out) if args["json"] else out


def test_hit_rate_is_cache_read_over_all_context(con, capsys):
    turn(con, T0, i=50_000, r=900_000, w=50_000, o=7_000)  # output isn't context: left out
    con.close()
    got = burn(capsys)
    assert got["hit_rate"] == pytest.approx(0.9)
    assert got["estimate"] is True and got["ttl_s"] == 3600
    assert got["tokens"] == {"input": 50_000, "cache_read": 900_000, "cache_write": 50_000}
    assert got["cost_usd"]["cache_read"] == pytest.approx(0.45)
    assert got["cost_usd"]["cache_write"] == pytest.approx(0.5)
    assert got["cost_usd"]["total"] == pytest.approx(0.25 + 0.45 + 0.5 + 0.175)


def test_turn_after_more_than_an_hour_idle_is_a_rewrite(con, capsys):
    turn(con, T0, w=1_000_000)                   # first turn of a session: a write, not a rewrite
    turn(con, T0 + 30 * MIN, w=100_000)
    turn(con, T0 + 30 * MIN + 2 * HOUR, w=500_000, r=10)  # back after 2h: the cache had expired
    turn(con, T0, session="s2", w=1_000_000)
    turn(con, T0 + HOUR, session="s2", w=300_000)  # exactly the TTL: not longer than it
    con.close()
    got = burn(capsys)
    assert got["idle_rewrites"] == {"turns": 1, "tok": 500_000, "cost_usd": pytest.approx(5.0)}
    assert [s["session"] for s in got["idle_sessions"]] == ["s1"]
    s1 = got["idle_sessions"][0]
    assert s1["idle_rewrites"] == 1 and s1["turns"] == 3 and s1["project"] == "p"
    assert s1["idle_rewrite_cost_usd"] == pytest.approx(5.0)
    assert s1["cost_usd"] == pytest.approx(16.0 + 0.000005)


def test_gap_is_per_session_not_across_sessions(con, capsys):
    turn(con, T0, session="a", w=100)
    turn(con, T0 + 3 * HOUR, session="b", w=100)  # another session: its first turn
    turn(con, T0 + 3 * HOUR + MIN, session="a", w=100)  # 'a' itself was idle 3h
    con.close()
    got = burn(capsys)
    assert got["idle_rewrites"]["turns"] == 1 and [s["session"] for s in got["idle_sessions"]] == ["a"]


def test_gap_before_since_still_counts_and_older_turns_stay_out(con, capsys):
    turn(con, T0, w=1_000_000)  # before the period: only the reference for the gap
    turn(con, T0 + 5 * HOUR, w=200_000)
    con.close()
    got = burn(capsys, since=(T0 + HOUR).isoformat())
    assert got["turns"] == 1 and got["tokens"]["cache_write"] == 200_000
    assert got["idle_rewrites"]["turns"] == 1 and got["idle_rewrites"]["cost_usd"] == pytest.approx(2.0)
    assert got["since"].startswith((T0 + HOUR).isoformat()[:16])


def test_synthetic_rows_are_not_turns_and_do_not_hide_a_gap(con, capsys):
    turn(con, T0, w=100)
    turn(con, T0 + 90 * MIN, model="<synthetic>")  # local notice: no tokens, no API call
    turn(con, T0 + 2 * HOUR, w=100)
    con.close()
    got = burn(capsys)
    assert got["turns"] == 2 and got["idle_rewrites"]["turns"] == 1
    assert got["unpriced_models"] == []  # nothing to price in a zero-token row


def test_projects_sorted_by_rewrite_cost(con, capsys):
    turn(con, T0, session="x1", project="cheap", r=1_000_000, w=10_000)
    turn(con, T0 + 2 * HOUR, session="x1", project="cheap", w=10_000)
    turn(con, T0, session="y1", project="pricey", w=10_000)
    turn(con, T0 + 2 * HOUR, session="y1", project="pricey", w=900_000)
    turn(con, T0, session="y2", project="pricey", r=500_000)
    con.close()
    got = burn(capsys)
    assert [p["project"] for p in got["projects"]] == ["pricey", "cheap"]
    pricey = got["projects"][0]
    assert pricey["sessions"] == 2 and pricey["turns"] == 3 and pricey["idle_rewrites"] == 1
    assert pricey["idle_rewrite_cost_usd"] == pytest.approx(9.0)
    assert pricey["cache_write_cost_usd"] == pytest.approx(9.1)
    assert pricey["hit_rate"] == pytest.approx(500_000 / 1_410_000)
    assert [s["session"] for s in got["idle_sessions"]] == ["y1", "x1"]


def test_compact_candidates_are_the_top_tier_of_long_sessions(con, capsys):
    for k in range(10):  # ten sessions of 10 turns, average context 60k..150k
        for t in range(10):
            turn(con, T0 + t * MIN, session=f"long{k}", r=60_000 + k * 10_000)
    for t in range(3):  # bigger context, but too short to be worth a /compact
        turn(con, T0 + t * MIN, session="short", r=900_000)
    con.close()
    got = burn(capsys)
    cp = got["compact"]
    assert cp["min_turns"] == 10 and cp["min_context_tok"] == 50_000
    assert cp["threshold_tok"] == pytest.approx(150_000)  # the top 10% of 10 is one session
    assert [(s["session"], s["turns"]) for s in cp["sessions"]] == [("long9", 10)]
    assert cp["sessions"][0]["avg_context_tok"] == pytest.approx(150_000)
    assert cp["sessions"][0]["cost_usd"] == pytest.approx(10 * 150_000 * 0.5 / 1e6)


@pytest.mark.parametrize(("n", "flagged"), [(5, 1), (19, 2), (20, 2), (21, 3)])
def test_compact_tier_is_the_top_tenth_rounded_up(con, capsys, n, flagged):
    # Was the nearest-rank 90th percentile and everything at or above it: 2 of 10, 3 of 20.
    for k in range(n):
        for t in range(10):
            turn(con, T0 + t * MIN, session=f"s{k:02d}", r=60_000 + k * 10_000)
    con.close()
    cp = burn(capsys)["compact"]
    assert [s["session"] for s in cp["sessions"]] == [f"s{k:02d}" for k in range(n - 1, n - 1 - flagged, -1)]


def test_compact_tier_keeps_sessions_tied_at_the_cutoff(con, capsys):
    for k in range(10):
        for t in range(10):
            turn(con, T0 + t * MIN, session=f"s{k}", r=200_000 if k >= 8 else 60_000)
    con.close()
    assert sorted(s["session"] for s in burn(capsys)["compact"]["sessions"]) == ["s8", "s9"]


def test_small_sessions_are_never_compact_candidates(con, capsys):
    for k in range(3):
        for t in range(12):
            turn(con, T0 + t * MIN, session=f"s{k}", r=20_000 + k * 1_000)
    con.close()
    cp = burn(capsys)["compact"]
    assert cp["threshold_tok"] == 50_000 and cp["sessions"] == []


def test_session_belongs_to_the_project_of_most_turns(con, capsys):
    for t in range(10):
        turn(con, T0 + t * MIN, project="main" if t < 7 else "side", r=200_000)
    con.close()
    got = burn(capsys)
    assert got["compact"]["sessions"][0]["project"] == "main"
    assert {p["project"]: p["turns"] for p in got["projects"]} == {"main": 7, "side": 3}


def test_surface_and_device_filters(con, capsys):
    turn(con, T0, surface="cli", w=1_000)
    turn(con, T0, session="v", surface="claude-vscode", w=3_000)
    con.close()
    assert burn(capsys, surface="vscode")["tokens"]["cache_write"] == 3_000
    assert burn(capsys, device=cmon._device_name())["tokens"]["cache_write"] == 4_000
    with pytest.raises(SystemExit, match="No Claude Code log data"):
        burn(capsys, device="no-such-machine")
    assert "· vscode only" in burn(capsys, surface="vscode", json=False)


def test_other_accounts_desktop_sessions_need_all_accounts(con, capsys):
    turn(con, T0, w=1_000)
    turn(con, T0, session="d", surface="claude-desktop", w=5_000)  # not tied to the terminal account
    con.close()
    assert burn(capsys)["tokens"]["cache_write"] == 1_000
    assert burn(capsys, all_accounts=True)["tokens"]["cache_write"] == 6_000
    assert "Excluded 0.0M tok" in burn(capsys, json=False)


def test_unpriced_model_costs_zero_and_warns(con, capsys):
    turn(con, T0, model="claude-mystery-9", w=2_000_000)
    con.close()
    cmon.burn(Namespace(by=None, since="all", json=False, all_accounts=False, plan=None, cache=True))
    cap = capsys.readouterr()
    assert "no price for claude-mystery-9 (2.0M tok)" in cap.err
    got = burn(capsys)
    assert got["unpriced_models"] == ["claude-mystery-9"] and got["cost_usd"]["cache_write"] == 0


def test_text_report(con, capsys):
    turn(con, T0, project="cmon", r=900_000, w=100_000)
    turn(con, T0 + 2 * HOUR, project="cmon", w=100_000)
    con.close()
    out = burn(capsys, json=False)
    assert "Cache efficiency (API equivalent, estimate):" in out
    assert "hit rate           81.8%   of the input context came from cache (2 turns, 1 session)" in out
    assert "idle rewrites         1 turns  US$      1.00    50% of cache write" in out
    assert "cmon" in out and "s1" in out and "assumes 1h" in out
    assert "/compact candidates" in out and "none in the period." in out


def test_text_report_without_rewrites_and_with_many_projects(con, capsys):
    for k in range(cmon.CACHE_TOP + 2):
        turn(con, T0, session=f"s{k}", project=f"project-{k:02d}-with-a-rather-long-name", r=1_000 + k)
    con.close()
    out = burn(capsys, json=False)
    assert "none: no session went on after more than 1h idle." in out
    assert "… 2 more (--json lists all)" in out
    assert "project-11-with-a-rathe…" in out and "project-00" not in out


def test_no_data_exits(con, capsys):
    con.close()
    with pytest.raises(SystemExit, match="No Claude Code log data in period"):
        burn(capsys)


@pytest.mark.parametrize("extra", [{"plan": "weekly"}, {"by": "project"}])
def test_cache_refuses_by_and_plan(con, capsys, extra):
    con.close()
    with pytest.raises(SystemExit, match="doesn't take --by or --plan"):
        burn(capsys, **extra)


def test_cli_flag_and_default_by(cli, install_claude_log):
    install_claude_log()
    res = cli("burn", "--cache", "--since", "all", "--json")
    assert res.exit_code == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["turns"] > 0 and 0 <= got["hit_rate"] <= 1
    assert cli("burn", "--cache", "--by", "model").exit_code == 1
    plain = cli("burn", "--since", "all", "--json")  # without --by, still grouped by model
    assert plain.exit_code == 0 and {g["grp"] for g in json.loads(plain.stdout)} >= {"claude-opus-fixture"}
