"""B2: the daily budget of the weekly limit in `cmon now`."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import cmon

WEEK_RESET = "2030-01-06T06:00:00+00:00"  # 4 days after the clock fixture's now (Wed 06:00 UTC)
D = datetime(2030, 1, 2, tzinfo=UTC)       # local midnight of "today" in UTC
NOW = datetime(2030, 1, 2, 6, tzinfo=UTC)
CYCLE = datetime(2029, 12, 30, 6, tzinfo=UTC)  # WEEK_RESET - 7 days
NO_READING = ("Today: ~11 pp budget for the rest of the day (equal split), no earlier reading to measure use "
              "— week at 41%, resets in 4d 0h.")


def at(day, hour, minute=0):
    return datetime(2030, 1, day, hour, minute, tzinfo=UTC)


# ---- _day_budget: the pure split -------------------------------------------------------------

def test_equal_split_over_whole_days():
    assert cmon._day_budget(40, D, D + timedelta(days=4), UTC) == pytest.approx(15)


def test_reset_day_counts_only_up_to_the_reset():
    assert cmon._day_budget(40, D, at(5, 12), UTC) == pytest.approx(60 / 3.5)


def test_reset_later_today_leaves_everything_to_today():
    assert cmon._day_budget(40, D, at(2, 15), UTC) == pytest.approx(60)


def test_day_under_way_counts_from_the_base_reading():
    assert cmon._day_budget(40, NOW, NOW + timedelta(days=4), UTC) == pytest.approx(60 * 18 / 96)


def test_weekday_weights_shift_the_share():
    def busy_wednesday(t):
        return 2.0 if t.weekday() == 2 else 1.0
    assert cmon._day_budget(40, D, D + timedelta(days=4), UTC, busy_wednesday) == pytest.approx(24)


def test_hour_shape_weighs_partial_days():
    def afternoons(t):
        return 1.0 if t.hour >= 12 else 0.0
    base, reset = at(2, 12), at(4, 12)
    assert cmon._day_budget(40, base, reset, UTC) == pytest.approx(15)  # 12h of 48h
    assert cmon._day_budget(40, base, reset, UTC, afternoons) == pytest.approx(30)  # 1 of 2 afternoons


def test_zero_profile_falls_back_to_equal_split():
    assert cmon._day_budget(40, D, D + timedelta(days=4), UTC, lambda t: 0.0) == pytest.approx(15)


@pytest.mark.parametrize("base_pct", [100.0, 120.0])
def test_nothing_left_means_no_budget(base_pct):
    assert cmon._day_budget(base_pct, D, D + timedelta(days=4), UTC) == 0.0


def test_reset_at_or_before_base_returns_what_is_left():
    assert cmon._day_budget(40, NOW, NOW, UTC) == 60
    assert cmon._day_budget(40, NOW, NOW - timedelta(hours=1), UTC) == 60


def test_dst_day_keeps_its_real_length():
    ny = ZoneInfo("America/New_York")  # 2030-03-10 springs forward: a 23h day
    base = datetime(2030, 3, 10, tzinfo=ny)
    reset = datetime(2030, 3, 12, tzinfo=ny)
    assert cmon._day_budget(40, base, reset, ny) == pytest.approx(60 * 23 / 47)


def test_local_midnight_bounds_today_not_utc_midnight():
    sp = ZoneInfo("America/Sao_Paulo")  # UTC-3
    assert cmon._day_budget(40, at(2, 3), at(4, 3), sp) == pytest.approx(30)
    # 01:00 UTC is 22:00 the day before in São Paulo: only 2h of that local day remain
    assert cmon._day_budget(40, at(2, 1), at(3, 1), sp) == pytest.approx(5)


# ---- _day_baseline: where today's count starts -----------------------------------------------

def test_midnight_interpolated_between_close_readings():
    readings = [(at(1, 23, 50), 35.0), (at(2, 0, 10), 37.0)]
    assert cmon._day_baseline(readings, D, CYCLE, NOW, 41.0) == (D, 36.0, "day")


def test_unmoved_percent_across_a_long_gap_is_exact():
    readings = [(at(1, 18), 35.0), (at(2, 5), 35.0)]
    assert cmon._day_baseline(readings, D, CYCLE, NOW, 41.0) == (D, 35.0, "day")


def test_moved_percent_across_a_long_gap_counts_from_first_reading_today():
    readings = [(at(1, 18), 30.0), (at(2, 5), 38.0)]
    assert cmon._day_baseline(readings, D, CYCLE, NOW, 41.0) == (at(2, 5), 38.0, "first")


def test_gap_limit_is_tunable():
    readings = [(at(1, 18), 30.0), (at(2, 5), 38.0)]
    t, pct, kind = cmon._day_baseline(readings, D, CYCLE, NOW, 41.0, max_gap_h=12)
    assert (t, kind) == (D, "day")
    assert pct == pytest.approx(30 + 8 * 6 / 11)


def test_only_the_live_reading_means_none():
    assert cmon._day_baseline([], D, CYCLE, NOW, 41.0) == (NOW, 41.0, "none")


def test_cycle_that_began_today_starts_at_zero():
    start = at(2, 2)
    assert cmon._day_baseline([(at(2, 4), 20.0)], D, start, NOW, 41.0) == (start, 0.0, "reset")


def test_falling_percent_inside_a_cycle_never_counts_negative_use():
    readings = [(at(1, 23, 50), 40.0), (at(2, 0, 10), 38.0)]
    assert cmon._day_baseline(readings, D, CYCLE, NOW, 41.0) == (D, 38.0, "day")


def test_unknown_cycle_start_without_earlier_readings():
    assert cmon._day_baseline([(at(2, 1), 39.0)], D, None, NOW, 41.0) == (at(2, 1), 39.0, "first")


def test_now_exactly_at_midnight():
    assert cmon._day_baseline([(at(1, 23), 40.0)], D, CYCLE, D, 41.0) == (D, 41.0, "day")


def test_readings_outside_the_cycle_or_not_before_now_are_ignored():
    readings = [(at(1, 5), 90.0), (NOW, 99.0), (NOW + timedelta(minutes=5), 99.0)]
    assert cmon._day_baseline(readings, D, at(1, 6), NOW, 41.0) == (NOW, 41.0, "none")


# ---- the line in `cmon now` ------------------------------------------------------------------

def _usage(weekly=41, reset=WEEK_RESET, session=True):
    limits = [{"kind": "session", "percent": 24, "resets_at": "2030-01-02T08:00:00+00:00",
               "is_active": True}] if session else []
    if weekly is not None:
        limits.append({"kind": "weekly_all", "percent": weekly, "resets_at": reset, "is_active": True})
    return {"limits": limits}


def _seed(*readings, reset=WEEK_RESET):
    con = cmon.db()
    con.executemany(
        f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [("claude", t, "weekly_all", "weekly", "All models", p, 10080, datetime.fromisoformat(reset), True,
          f"b2-seed-{i}", None) for i, (t, p) in enumerate(readings)])
    con.close()


@pytest.fixture
def run_now(cli, clock, monkeypatch):
    def run(usage=None):
        monkeypatch.setattr(cmon, "fetch", lambda retries=cmon.RETRIES: usage or _usage())
        result = cli("now")
        assert result.exit_code == 0, result.stderr
        return result.stdout
    return run


def _today(out):
    lines = [x for x in out.splitlines() if x.startswith("Today")]
    assert len(lines) <= 1
    return lines[0] if lines else None


def test_line_with_a_reading_at_midnight(run_now):
    _seed((at(1, 23, 50), 35), (at(2, 0, 10), 35))
    out = run_now()
    assert _today(out) == ("Today: 6 pp used of ~15 pp budget (equal split), 9 pp left — week at 41%, "
                           "resets in 4d 0h.")
    assert out.index("Today:") < out.index("5h window:")


def test_line_over_budget(run_now):
    _seed((at(1, 23, 50), 20), (at(2, 0, 10), 20))
    assert _today(run_now()) == ("Today: 21 pp used of ~19 pp budget (equal split), 2 pp over — week at 41%, "
                                 "resets in 4d 0h.")


def test_line_budget_reached(run_now):
    _seed((at(1, 23, 50), 23), (at(2, 0, 10), 23))
    assert _today(run_now()).startswith("Today: 18 pp used of ~18 pp budget (equal split), budget reached —")


def test_line_profile_weighted_when_the_learned_profile_passes_its_gate(run_now, monkeypatch):
    dow = [1.0] * 7
    dow[2] = 3.0  # Wednesday, the clock fixture's day
    monkeypatch.setattr(cmon, "_hour_profile", lambda con, tz, as_of=None: ([1.0] * 24, dow, 1.0, "learned"))
    _seed((at(1, 23, 50), 35), (at(2, 0, 10), 35))
    # 3 + 1 + 1 + 1 full days + Sunday's 6h before the reset: today is 72 of 150 weighted hours
    assert _today(run_now()) == ("Today: 6 pp used of ~31 pp budget (profile-weighted), 25 pp left — "
                                 "week at 41%, resets in 4d 0h.")


def test_line_equal_split_with_only_a_quiet_hours_profile(run_now, monkeypatch):
    monkeypatch.setattr(cmon, "_hour_profile",
                        lambda con, tz, as_of=None: ([0.05] * 6 + [1.0] * 18, [1.0] * 7, 1.0, "quiet_hours"))
    _seed((at(1, 23, 50), 35), (at(2, 0, 10), 35))
    assert "~15 pp budget (equal split)" in _today(run_now())


def test_no_line_without_a_weekly_window(run_now):
    out = run_now(_usage(weekly=None))
    assert _today(out) is None
    assert "5h window: 24% used" in out


def test_no_line_when_the_weekly_window_has_no_reset(run_now):
    assert _today(run_now(_usage(reset=None))) is None


def test_line_without_a_session_window(run_now):
    _seed((at(1, 23, 50), 35), (at(2, 0, 10), 35))
    out = run_now(_usage(session=False))
    assert _today(out).startswith("Today: 6 pp used of ~15 pp budget")
    assert "5h window" not in out


def test_line_with_no_history_today(run_now):
    assert _today(run_now()) == NO_READING


def test_line_counts_from_first_reading_today_after_a_long_gap(run_now):
    _seed((at(1, 18), 30), (at(2, 5), 38))
    assert _today(run_now()) == ("Today since 05:00: 3 pp used of ~12 pp budget (equal split), 9 pp left — "
                                 "week at 41%, resets in 4d 0h.")


def test_line_ignores_readings_of_another_cycle(run_now):
    _seed((at(1, 23, 50), 10), (at(2, 0, 10), 10), reset="2030-01-05T06:00:00+00:00")
    assert _today(run_now()) == NO_READING


def test_line_when_the_reset_is_later_today(run_now):
    reset = "2030-01-02T20:00:00+00:00"
    _seed((at(1, 23, 50), 35), (at(2, 0, 10), 35), reset=reset)
    assert _today(run_now(_usage(reset=reset))) == (
        "Today: 6 pp used of ~65 pp budget (all that's left), 59 pp left — week at 41%, resets in 14h 0m.")


def test_line_when_the_reset_is_later_today_without_history(run_now):
    out = run_now(_usage(reset="2030-01-02T20:00:00+00:00"))
    assert _today(out) == ("Today: ~59 pp budget for the rest of the day (all that's left), no earlier reading "
                           "to measure use — week at 41%, resets in 14h 0m.")


def test_line_when_the_cycle_began_today(run_now):
    out = run_now(_usage(reset="2030-01-09T02:00:00+00:00"))  # began 2030-01-02 02:00
    assert _today(out) == ("Today since the 02:00 reset: 41 pp used of ~13 pp budget (equal split), "
                           "28 pp over — week at 41%, resets in 6d 20h.")


def test_line_when_the_weekly_limit_is_used_up(run_now):
    assert _today(run_now(_usage(weekly=100))) == "Today: weekly limit used up — resets in 4d 0h."


def test_line_uses_the_configured_timezone(run_now, monkeypatch):
    monkeypatch.setenv("CMON_TZ", "America/Sao_Paulo")  # today began at 03:00 UTC
    _seed((at(2, 0, 30), 33), (at(2, 2, 50), 36), (at(2, 3, 10), 36))
    assert _today(run_now()) == ("Today: 5 pp used of ~16 pp budget (equal split), 11 pp left — week at 41%, "
                                 "resets in 4d 0h.")


def test_line_survives_an_unavailable_database(run_now, monkeypatch):
    monkeypatch.setattr(cmon, "_read_db", lambda: None)
    _seed((at(1, 23, 50), 35), (at(2, 0, 10), 35))
    assert _today(run_now()) == NO_READING


def test_line_survives_a_failing_database():
    class Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("database gone")
    rows = cmon.limits(_usage())
    assert cmon._day_budget_line(Broken(), rows, NOW, UTC) == NO_READING


def test_fmt_until():
    assert cmon._fmt_until(timedelta(days=3, hours=22, minutes=5)) == "3d 22h"
    assert cmon._fmt_until(timedelta(hours=5, minutes=10)) == "5h 10m"
    assert cmon._fmt_until(timedelta(minutes=40)) == "40m"
    assert cmon._fmt_until(timedelta(seconds=-5)) == "0m"
