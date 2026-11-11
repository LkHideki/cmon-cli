from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import cmon

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def _seed(rows) -> None:
    """rows: (hours after T0, key, percent, resets_at as hours after T0 or None)."""
    con = cmon.db()
    out = []
    for n, (h, key, pct, reset_h) in enumerate(rows):
        role, minutes, label = (("5h", 300, "Current session") if key == "session"
                                else ("weekly", 10080, "All models"))
        reset = T0 + timedelta(hours=reset_h) if reset_h is not None else None
        out.append(("claude", T0 + timedelta(hours=h), key, role, label, pct, minutes, reset, True,
                    f"scoreboard-{n}", None))
    con.executemany(f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    con.close()


def _pairs(points, week_reset_a=6.5, week_reset_b=7 * 24 + 7):
    """points: (hours, session %, weekly %), both windows read at the same instant as collect does.
    The weekly window resets between 6h and 7h, where its percent drops."""
    rows = []
    for h, s, w in points:
        rows.append((h, "session", s, h + 5))
        rows.append((h, "weekly_all", w, week_reset_a if h < 7 else week_reset_b))
    return rows


# Weekly A (0h-6h): the 5h window sits at 100% from 1h to 3h (2h measured), resets at 4h, hits
# 100% at 5h with a single reading (a hit, no hours), resets at 6h. Weekly B (from 7h): the 5h
# window reaches 100% at 7h and is still there at 8h (1h).
POINTS = [(0, 50, 10), (1, 100, 20), (2, 100, 30), (3, 100, 40), (4, 10, 50), (5, 100, 60),
          (6, 20, 90), (7, 100, 5), (8, 100, 10)]


def test_scoreboard_json_per_weekly_cycle(cli, clock) -> None:
    _seed(_pairs(POINTS))
    clock.set(T0 + timedelta(hours=9))
    result = cli("trends", "--scoreboard", "--json")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"start": "2030-01-01T00:00:00+00:00", "peak_pct": 90.0, "leftover_pct": 10.0,
         "in_progress": False, "blocked_h": 2.0, "blocked_5h_cycles": 2},
        {"start": "2030-01-01T07:00:00+00:00", "peak_pct": 10.0, "leftover_pct": 90.0,
         "in_progress": True, "blocked_h": 1.0, "blocked_5h_cycles": 1},
    ]


def test_scoreboard_text(cli, clock) -> None:
    _seed(_pairs(POINTS))
    clock.set(T0 + timedelta(hours=9))
    result = cli("trends", "--scoreboard")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "Weekly (All models) — last 2 cycle(s):",
        "  start                 peak  left at reset  5h blocked  5h cycles at 100%",
        "  2030-01-01 00:00 UTC   90%            10%        2.0h                  2",
        "  2030-01-01 07:00 UTC   10%            90%        1.0h                  1  in progress",
        "Blocked time is measured between snapshots: a block shorter than the collection interval "
        "doesn't show.",
    ]


def test_scoreboard_last_cycle_already_reset_is_not_in_progress(cli, clock) -> None:
    _seed(_pairs(POINTS))
    clock.set(T0 + timedelta(days=8))  # past weekly B's resets_at, with no reading since
    rows = json.loads(cli("trends", "--scoreboard", "--json").stdout)
    assert [r["in_progress"] for r in rows] == [False, False]
    assert "in progress" not in cli("trends", "--scoreboard").stdout


def test_scoreboard_keeps_the_last_eight_cycles_oldest_first(cli, clock) -> None:
    rows = []
    for c in range(10):  # ten weekly cycles, one a day, each peaking at 10 + c
        rows += [(c * 24, "weekly_all", 1.0, None), (c * 24 + 12, "weekly_all", 10.0 + c, None)]
    _seed(rows)
    clock.set(T0 + timedelta(days=11))
    out = json.loads(cli("trends", "--scoreboard", "--json").stdout)
    assert len(out) == cmon.SCOREBOARD_CYCLES == 8
    assert [r["peak_pct"] for r in out] == [12.0 + i for i in range(8)]
    assert out[0]["start"] == "2030-01-03T00:00:00+00:00"
    assert [r["in_progress"] for r in out] == [False] * 7 + [True]  # no resets_at: assume open
    assert all(r["blocked_h"] == 0 and r["blocked_5h_cycles"] == 0 for r in out)


def test_scoreboard_splits_a_block_across_the_weekly_reset(cli, clock) -> None:
    # Weekly readings at 0h and 2h (after a drop): the 5h block from 1h to 3h counts 1h in each.
    _seed([(0, "weekly_all", 50, None), (2, "weekly_all", 1, None),
           (0, "session", 10, None), (1, "session", 100, None), (3, "session", 100, None)])
    clock.set(T0 + timedelta(hours=4))
    out = json.loads(cli("trends", "--scoreboard", "--json").stdout)
    assert [(r["blocked_h"], r["blocked_5h_cycles"]) for r in out] == [(1.0, 1), (1.0, 0)]


def test_scoreboard_ignores_gaps_longer_than_the_5h_window(cli, clock) -> None:
    # No reading for 6h while at 100%: a reset went unseen, so that gap isn't blocked time.
    _seed([(0, "weekly_all", 10, None), (1, "weekly_all", 20, None), (7, "weekly_all", 30, None),
           (7.5, "weekly_all", 30, None),
           (0, "session", 100, None), (1, "session", 100, None), (7, "session", 100, None),
           (7.5, "session", 100, None)])
    clock.set(T0 + timedelta(hours=8))
    (row,) = json.loads(cli("trends", "--scoreboard", "--json").stdout)
    assert row["blocked_h"] == 1.5  # 0h-1h and 7h-7.5h; 1h-7h dropped
    assert row["blocked_5h_cycles"] == 1  # one cycle as _cycles sees it (no drop observed)


def test_scoreboard_splits_at_a_reset_with_no_drop(cli, clock) -> None:
    # Machine off across the weekly reset at 168h; the new cycle's first reading (15%) is above the
    # old one's last (10%), so only resets_at shows the new cycle. Jitter under an hour doesn't cut.
    _seed([(0, "weekly_all", 2, 168), (24, "weekly_all", 10, 168 + 0.5 / 3600),
           (170, "weekly_all", 15, 336), (180, "weekly_all", 20, 336)])
    clock.set(T0 + timedelta(hours=190))
    out = json.loads(cli("trends", "--scoreboard", "--json").stdout)
    assert [(r["start"], r["peak_pct"], r["leftover_pct"], r["in_progress"]) for r in out] == [
        ("2030-01-01T00:00:00+00:00", 10.0, 90.0, False),
        ("2030-01-08T02:00:00+00:00", 20.0, 80.0, True),
    ]


def test_scoreboard_without_weekly_history(cli, clock) -> None:
    _seed([(0, "session", 40, 5)])
    text = cli("trends", "--scoreboard")
    assert text.exit_code == 1
    assert "No weekly cycles yet" in text.stderr
    js = cli("trends", "--scoreboard", "--json")
    assert (js.exit_code, js.stdout.strip()) == (0, "[]")


def test_scoreboard_without_database(cli, clock) -> None:
    result = cli("trends", "--scoreboard", "--json")
    assert result.exit_code == 1
    assert "No history" in result.stderr
    assert result.stdout == ""
