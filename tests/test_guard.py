import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

import cmon

RESET = "2030-01-02T08:00:00+00:00"  # 2h after the clock fixture's now
WEEK_RESET = "2030-01-05T06:00:00+00:00"
PAST = "2030-01-02T05:30:00+00:00"  # already behind the clock


def _cache(clock, rows=None, age_min=5):
    rows = rows if rows is not None else [
        ("session", "Current session", 42.0, RESET, True),
        ("weekly_all", "All models", 91.0, WEEK_RESET, True),
        ("weekly_scoped:Fable", "Fable only", 10.0, None, False),
    ]
    cmon._write_cache(rows, clock.instant - timedelta(minutes=age_min))


@pytest.fixture
def no_backend(monkeypatch):
    """guard must answer from the cache alone: any API call or DuckDB open fails the test."""
    def boom(*_a, **_k):
        raise AssertionError("guard must not touch the API or the database")
    for name in ("fetch", "db", "_read_db", "_rows_from_db", "_status_fetch"):
        monkeypatch.setattr(cmon, name, boom)


def _json(result):
    return json.loads(result.stdout)


def test_under_max_exits_0_with_one_line_on_stderr(cli, clock, no_backend):
    _cache(clock)
    result = cli("guard", "--max", "90")
    assert result.exit_code == 0 and result.stdout == ""
    assert result.stderr == "cmon guard: 5h window at 42%, under --max 90; resets in 2h 0m.\n"


@pytest.mark.parametrize("pct", [90.0, 97.5, 100.0])
def test_at_or_above_max_exits_1(cli, clock, no_backend, pct):
    _cache(clock, [("session", "Current session", pct, RESET, True)])
    result = cli("guard", "--max", "90")
    assert result.exit_code == 1 and "at or above --max 90" in result.stderr


@pytest.mark.parametrize(("window", "key", "code"), [
    ("5h", "session", 0), ("SESSION", "session", 0), ("weekly", "weekly_all", 1),
    ("weekly_all", "weekly_all", 1), ("fable", "weekly_scoped:Fable", 0),
    ("weekly_scoped:Fable", "weekly_scoped:Fable", 0),
])
def test_window_names_and_aliases(cli, clock, no_backend, window, key, code):
    _cache(clock)
    result = cli("guard", "--window", window, "--max", "90", "--json")
    assert result.exit_code == code and _json(result)["window"] == key


def test_json_shape(cli, clock, no_backend):
    _cache(clock)
    result = cli("guard", "--window", "weekly", "--max", "85.5", "--json", "--quiet")
    assert result.exit_code == 1 and result.stderr == ""
    assert _json(result) == {"window": "weekly_all", "percent": 91.0, "max": 85.5,
                             "resets_at": WEEK_RESET, "age_s": 300, "result": "over"}


def test_quiet_silences_stderr_on_every_outcome(cli, clock, no_backend):
    assert cli("guard", "--max", "90", "-q").as_dict() == {"exit_code": 3, "stdout": "", "stderr": ""}
    _cache(clock)
    assert cli("guard", "--max", "90", "--quiet").as_dict() == {"exit_code": 0, "stdout": "", "stderr": ""}


def test_window_that_already_reset_counts_as_zero_without_calling_the_api(cli, clock, no_backend):
    # 'status' would fetch here (dead cycle); guard must not, and the old 99% no longer applies.
    _cache(clock, [("session", "Current session", 99.0, PAST, True)])
    result = cli("guard", "--max", "50", "--json")
    assert result.exit_code == 0
    assert _json(result) | {"age_s": None} == {"window": "session", "percent": 0.0, "max": 50.0,
                                               "resets_at": None, "age_s": None, "result": "under"}
    assert "already reset; counting it as 0%" in result.stderr


def test_parked_window_without_reset_uses_cached_percent(cli, clock, no_backend):
    _cache(clock, [("session", "Current session", 0.0, None, False)])
    result = cli("guard", "--max", "1", "--json")
    assert result.exit_code == 0 and _json(result)["percent"] == 0.0
    assert "resets in" not in result.stderr


def test_no_cache_is_3_not_a_free_window(cli, no_backend):
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3
    assert _json(result) == {"window": "session", "percent": None, "max": 90.0, "resets_at": None,
                             "age_s": None, "result": "no_data"}
    assert "no cached usage" in result.stderr


@pytest.mark.parametrize("content", [
    "{not json", '{"version": 2, "providers": {"claude": {}}}',
    '{"version": 2, "providers": {"claude": {"updated_at": "2030-01-02T05:55:00+00:00", "windows": {}}}}',
    # JSON, but not an object, or nested past the parser's limit: was a traceback and exit 1,
    # which a hook reads as "over --max".
    "[]", "null", '"x"', "1", pytest.param("[" * 100_000 + "]" * 100_000, id="deep-nesting"),
])
def test_unreadable_or_empty_cache_is_3(cli, clock, no_backend, paths, content):
    paths.cache.parent.mkdir(parents=True, exist_ok=True)
    paths.cache.write_text(content)
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3 and _json(result)["result"] == "no_data"


def test_malformed_percent_is_3(cli, clock, no_backend):
    _cache(clock, [("session", "Current session", "lots", RESET, True)])
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3 and _json(result)["result"] == "no_data"
    assert "no usable percent" in result.stderr


def _refuse(token):
    raise AssertionError(f"--json printed {token}, which isn't JSON")


@pytest.mark.parametrize("pct", [math.nan, math.inf, -math.inf, 10 ** 400],
                         ids=["nan", "inf", "-inf", "int-past-float"])
def test_non_finite_or_huge_percent_is_3_with_valid_json(cli, clock, no_backend, pct):
    # NaN passed as "under" (exit 0), Infinity as "over", and a 401-digit int was an OverflowError.
    _cache(clock, [("session", "Current session", pct, RESET, True)])
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3 and "no usable percent" in result.stderr
    assert json.loads(result.stdout, parse_constant=_refuse)["result"] == "no_data"


def test_unexpected_error_is_3_not_1(cli, clock, no_backend, monkeypatch):
    def boom(*_a):
        raise RuntimeError("boom")
    monkeypatch.setattr(cmon, "_read_cache", boom)
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3 and _json(result)["result"] == "no_data"
    assert result.stderr == "cmon guard: couldn't read the cache (boom).\n"


def test_invalid_number_in_the_environment_is_3_for_guard_and_a_message_elsewhere(cli, clock, no_backend,
                                                                                  monkeypatch):
    _cache(clock)
    monkeypatch.setattr(cmon, "_ENV_ERRORS", ["CMON_ALERT_LEAD='30m' is not a whole number"])
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3 and _json(result)["result"] == "no_data"
    assert result.stderr == "cmon guard: CMON_ALERT_LEAD='30m' is not a whole number.\n"
    other = cli("status")
    assert other.as_dict() == {"exit_code": 1, "stdout": "",
                               "stderr": "cmon: CMON_ALERT_LEAD='30m' is not a whole number.\n"}


def test_env_number_records_a_bad_value_instead_of_raising(monkeypatch):
    errors = []
    monkeypatch.setattr(cmon, "_ENV_ERRORS", errors)
    monkeypatch.setenv("CMON_X", "")
    monkeypatch.setenv("CMON_Y", "2.5")
    assert cmon._env_number("CMON_X", 60) == 60
    assert cmon._env_number("CMON_Y", 1.0, float) == 2.5
    assert cmon._env_number("CMON_UNSET_FOR_TEST", 7) == 7
    assert errors == ["CMON_X='' is not a whole number"]


def test_invalid_env_number_in_a_fresh_process_is_3(paths):
    """The variables are read at import, before main(): this was a traceback with exit 1."""
    cmon._write_cache([("session", "Current session", 10.0, None, True)], datetime.now(UTC))
    env = {k: v for k, v in os.environ.items() if not k.startswith("CMON_") and k != "CLAUDE_OAUTH_TOKEN"}
    env.update(HOME=str(paths.home), CMON_DB=str(paths.db), CMON_ALERT_LEAD="")
    out = subprocess.run([sys.executable, cmon.__file__, "guard", "--max", "90", "--json"], cwd=paths.root,
                         env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 3, out.stderr
    assert json.loads(out.stdout)["result"] == "no_data"
    assert out.stderr == "cmon guard: CMON_ALERT_LEAD='' is not a whole number.\n"


@pytest.mark.parametrize("reset",["garbage", "2030-01-02T08:00:00"])  # unparseable; no offset
@pytest.mark.parametrize(("pct", "code", "result"), [(10.0, 0, "under"), (95.0, 1, "over")])
def test_unreadable_reset_judges_the_percent_alone(cli, clock, no_backend, reset, pct, code, result):
    # Was a traceback and exit 1, which a hook reads as "over --max" even at 10%.
    _cache(clock, [("session", "Current session", pct, reset, True)])
    out = cli("guard", "--max", "80", "--json")
    assert out.exit_code == code
    assert _json(out) == {"window": "session", "percent": pct, "max": 80.0, "resets_at": None,
                          "age_s": 300, "result": result}
    assert "resets in" not in out.stderr


def test_missing_window_is_3_and_lists_known_ones(cli, clock, no_backend):
    _cache(clock)
    result = cli("guard", "--window", "opus", "--max", "90", "--json")
    assert result.exit_code == 3
    assert _json(result) | {"age_s": 0} == {"window": "opus", "percent": None, "max": 90.0,
                                            "resets_at": None, "age_s": 0, "result": "missing"}
    assert "known: session, weekly_all, weekly_scoped:Fable" in result.stderr


def test_stale_cache_is_3_and_stale_min_moves_the_limit(cli, clock, no_backend):
    _cache(clock, age_min=61)
    result = cli("guard", "--max", "90", "--json")
    assert result.exit_code == 3
    assert _json(result) == {"window": "session", "percent": None, "max": 90.0, "resets_at": None,
                             "age_s": 3660, "result": "stale"}
    assert "61 min old, over --stale-min 60" in result.stderr
    assert cli("guard", "--max", "90", "--stale-min", "90").exit_code == 0


def test_stale_wins_over_a_reset_that_passed(cli, clock, no_backend):
    # An old cache can't vouch that the new cycle is still unused.
    _cache(clock, [("session", "Current session", 99.0, PAST, True)], age_min=120)
    assert cli("guard", "--max", "90").exit_code == 3


@pytest.mark.parametrize("args", [
    [], ["--max", "0"], ["--max", "-5"], ["--max", "101"], ["--max", "abc"], ["--max", "nan"],
    ["--max", "90", "--stale-min", "0"], ["--max", "90", "--stale-min", "x"],
])
def test_bad_arguments_are_usage_errors_exit_2(cli, no_backend, args):
    result = cli("guard", *args)
    assert result.exit_code == 2 and "cmon guard: error:" in result.stderr


def test_reads_cache_v1(cli, clock, no_backend, paths):
    paths.cache.parent.mkdir(parents=True, exist_ok=True)
    paths.cache.write_text(json.dumps({"ts": (clock.instant - timedelta(minutes=1)).isoformat(),
                                       "rows": [["session", "Current session", 95.0, RESET, True]]}))
    assert cli("guard", "--max", "90").exit_code == 1
