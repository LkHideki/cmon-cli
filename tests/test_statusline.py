import io
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = datetime(2030, 1, 2, 6, 0, tzinfo=UTC)  # the clock fixture's instant
H = timedelta(hours=1)
SESSION_RESET = T0 + 2 * H  # 5h cycle began at 03:00
WEEK_RESET = T0 + 72 * H  # weekly cycle began 2029-12-29 06:00
_n = 0


def snap(con, at, pct, key, reset):
    role, minutes = ("five_hour", 300) if key == "session" else ("weekly", 10080)
    con.execute(f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", at, key, role, key, pct, minutes, reset, True, None, None])


def tok(con, at, session, out=1_000_000):
    """One message of `out` Opus output tokens: US$ 25 per million."""
    global _n
    _n += 1
    con.execute(f"INSERT INTO token_log({cmon._TOKEN_LOG_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", f"m{_n}:r", at, "claude-opus-4-1", "cli", "p", session, 0, out, 0, 0, 0, 0, None])


def rows(session_pct=20.0, week_pct=54.0, session_reset=SESSION_RESET):
    return [("session", "Current session", session_pct, session_reset and session_reset.isoformat(), True),
            ("weekly_all", "All models", week_pct, WEEK_RESET.isoformat(), True)]


@pytest.fixture
def con(home, clock):
    c = cmon.db()
    yield c
    c.close()


@pytest.fixture
def history(con):
    """Session A: 11 pp of the 5h cycle, 27.4 pp of the week, US$ 125 over its whole life (one
    message predates the weekly cycle). B: 7 pp and 25.8 pp, US$ 50. A transcript without a
    sessionId takes its part of the rise but isn't cached; a month-old session isn't either."""
    for key, first, second in (("session", 10, 20), ("weekly_all", 50, 54)):
        reset = SESSION_RESET if key == "session" else WEEK_RESET
        snap(con, T0 - 2.5 * H, first, key, reset)
        snap(con, T0 - 0.5 * H, second, key, reset)
    tok(con, T0 - 10 * 24 * H, "A")
    tok(con, T0 - 30 * 24 * H, "Z")
    tok(con, T0 - 2 * H - 50 * timedelta(minutes=1), "A")  # 03:10
    tok(con, T0 - 2 * H - 40 * timedelta(minutes=1), "B")  # 03:20
    tok(con, T0 - 2 * H, "A", out=3_000_000)                # 04:00
    tok(con, T0 - 1.5 * H, "?")                             # 04:30
    tok(con, T0 - H, "B")                                   # 05:00
    return con


@pytest.fixture
def stdin(monkeypatch):
    def feed(payload):
        raw = payload if isinstance(payload, bytes | str) else json.dumps(payload)
        raw = raw if isinstance(raw, bytes) else raw.encode()
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    feed("")
    return feed


@pytest.fixture
def no_backend(monkeypatch):
    """statusline must answer from files alone: any API call, DuckDB open or log scan fails."""
    def boom(*_a, **_k):
        raise AssertionError("statusline must not touch the API, the database or the transcripts")
    for name in ("fetch", "db", "_read_db", "_rows_from_db", "_status_fetch", "_db_retry", "scan_logs"):
        monkeypatch.setattr(cmon, name, boom)


def cache(clock, age_min=5, **kw):
    cmon._write_cache(rows(**kw), clock.instant - timedelta(minutes=age_min))


def sessions(windows=None, **entries):
    windows = windows or {"session": SESSION_RESET, "weekly_all": WEEK_RESET}
    cmon._atomic_write_json(cmon._sessions_cache(), {
        "version": 1, "computed_at": T0.isoformat(),
        "windows": {k: {"resets_at": v.isoformat()} for k, v in windows.items()},
        "sessions": entries})


def line(cli):
    result = cli("statusline")
    assert result.exit_code == 0 and result.stderr == ""
    return result.stdout


def epoch(dt):
    return int(dt.timestamp())


def test_collect_cache_gives_the_session_its_cost_and_points(history, clock, cli, stdin, request):
    cmon._refresh_session_cost(history, rows(), T0)
    cache(clock)
    request.getfixturevalue("no_backend")
    stdin({"session_id": "A", "model": {"id": "claude-opus-4-1"}})
    assert line(cli) == "5h 20% 2h0m · wk 54% · session $125.00 (5h 11pp, wk 27.4pp)\n"
    stdin({"session_id": "B"})
    assert line(cli) == "5h 20% 2h0m · wk 54% · session $50.00 (5h 7pp, wk 25.8pp)\n"


def test_session_cache_contents(history):
    cmon._refresh_session_cost(history, rows(), T0)
    with open(cmon._sessions_cache(), encoding="utf-8") as f:
        data = json.load(f)
    assert set(data["sessions"]) == {"A", "B"}  # no '?', no session idle since before the week
    assert data["windows"] == {"session": {"resets_at": SESSION_RESET.isoformat()},
                               "weekly_all": {"resets_at": WEEK_RESET.isoformat()}}
    a = data["sessions"]["A"]
    assert a["cost_usd"] == 125.0 and a["pp"] == {"session": 11.0, "weekly_all": 27.4}
    assert datetime.fromisoformat(a["last_at"]) == T0 - 2 * H
    assert os.stat(cmon._sessions_cache()).st_mode & 0o077 == 0


def test_session_cache_keeps_the_most_recent_sessions(history, monkeypatch):
    monkeypatch.setattr(cmon, "SESSIONS_KEEP", 1)
    cmon._refresh_session_cost(history, rows(), T0)
    with open(cmon._sessions_cache(), encoding="utf-8") as f:
        assert set(json.load(f)["sessions"]) == {"B"}  # B's last message (05:00) is the latest


def test_session_cache_needs_a_cycle(history):
    cmon._refresh_session_cost(history, [("session", "Current session", 0.0, None, False)], T0)
    assert not os.path.exists(cmon._sessions_cache())


def test_session_cache_failure_is_reported_not_raised(capsys):
    class Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("disk gone")
    cmon._refresh_session_cost(Broken(), rows(), T0)
    assert "session cost skipped: disk gone" in capsys.readouterr().err
    assert not os.path.exists(cmon._sessions_cache())


def test_collect_writes_the_cache_statusline_reads(history, clock, cli, stdin, monkeypatch, capsys):
    history.close()
    monkeypatch.setattr(cmon, "fetch", lambda: {"limits": [
        {"kind": "session", "percent": 25, "resets_at": SESSION_RESET.isoformat(), "is_active": True},
        {"kind": "weekly_all", "percent": 54, "resets_at": WEEK_RESET.isoformat(), "is_active": True}]})
    cmon.collect(Namespace(force=True, alert=False))
    capsys.readouterr()
    stdin({"session_id": "A"})
    # The rise to 25% came with nothing logged: it goes to 'other', A keeps its 11 pp.
    assert line(cli) == "5h 25% 2h0m · wk 54% · session $125.00 (5h 11pp, wk 27.4pp)\n"


def test_live_cost_from_stdin_wins_over_the_cached_one(clock, cli, stdin, no_backend):
    cache(clock)
    sessions(A={"cost_usd": 125.0, "pp": {"session": 11.0}})
    stdin({"session_id": "A", "cost": {"total_cost_usd": 3.104}})
    assert line(cli) == "5h 20% 2h0m · wk 54% · session $3.10 (5h 11pp)\n"


def test_unknown_session_shows_only_the_plan_or_the_live_cost(clock, cli, stdin, no_backend):
    cache(clock)
    sessions(A={"cost_usd": 1.0, "pp": {"session": 1.0}})
    stdin({"session_id": "new"})
    assert line(cli) == "5h 20% 2h0m · wk 54%\n"
    stdin({"session_id": "new", "cost": {"total_cost_usd": 0.42}})
    assert line(cli) == "5h 20% 2h0m · wk 54% · session $0.42\n"


def test_points_without_cost_still_show(clock, cli, stdin, no_backend):
    cache(clock)
    sessions(A={"cost_usd": None, "pp": {"weekly_all": 0.4}})
    stdin({"session_id": "A"})
    assert line(cli) == "5h 20% 2h0m · wk 54% · session wk 0.4pp\n"


@pytest.mark.parametrize("payload", [
    "", "   \n", "not json", "[1, 2]", "null", '{"session_id": 5}', '{"session_id": ""}',
    '{"session_id": "A", "cost": {"total_cost_usd": "3"}}', '{"session_id": "A", "cost": [1]}',
    '{"session_id": "A", "cost": {"total_cost_usd": NaN}}', '{"cost": {"total_cost_usd": -1}}',
    '{"rate_limits": "x"}', '{"rate_limits": {"five_hour": {"used_percentage": true}}}',
    '{"rate_limits": {"five_hour": {"used_percentage": "9"}, "seven_day": []}}',
    b"\xff\xfe garbage", "\ufeff{}",
    # nested past the parser's recursion limit: RecursionError, not a ValueError
    pytest.param("[" * 200_000, id="deep-array"), pytest.param('{"a":' * 100_000, id="deep-object"),
])
def test_bad_stdin_prints_the_plan_part(clock, cli, stdin, no_backend, payload):
    cache(clock)
    sessions(A={"cost_usd": 9.0, "pp": {"session": 1.0}})
    stdin(payload)
    out = line(cli)
    assert out.startswith("5h 20% 2h0m · wk 54%")
    assert "$3" not in out and "$-" not in out and "nan" not in out.lower()


def test_terminal_stdin_is_not_read(clock, cli, monkeypatch, no_backend):
    class Tty:
        def isatty(self):
            return True

        def read(self, *_a):
            raise AssertionError("statusline would block on a terminal")
        buffer = property(lambda self: self)

    monkeypatch.setattr(sys, "stdin", Tty())
    cache(clock)
    assert line(cli) == "5h 20% 2h0m · wk 54%\n"


def test_closed_stdin_is_no_input(clock, cli, monkeypatch, no_backend):
    monkeypatch.setattr(sys, "stdin", None)
    cache(clock)
    assert line(cli) == "5h 20% 2h0m · wk 54%\n"


def test_nothing_anywhere_prints_offline_and_exits_0(clock, cli, stdin, no_backend):
    assert line(cli) == "cmon offline\n"


def test_without_cache_the_plan_comes_from_stdin_rate_limits(clock, cli, stdin, no_backend):
    stdin({"session_id": "A", "rate_limits": {
        "five_hour": {"used_percentage": 23.4, "resets_at": epoch(T0 + 1.5 * H)},
        "seven_day": {"used_percentage": 41, "resets_at": epoch(WEEK_RESET)}}})
    assert line(cli) == "5h 23% 1h30m · wk 41%\n"


def test_fresh_cache_wins_over_stdin_rate_limits(clock, cli, stdin, no_backend):
    cache(clock, age_min=10)
    stdin({"rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": epoch(T0 + 1.5 * H)}}})
    assert line(cli) == "5h 20% 2h0m · wk 54%\n"


def test_stale_cache_says_its_age_unless_stdin_has_the_window(clock, cli, stdin, no_backend):
    cache(clock, age_min=45)
    assert line(cli) == "5h 20% 2h0m · wk 54% · 45m ago\n"
    stdin({"rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": epoch(T0 + 1.5 * H)}}})
    assert line(cli) == "5h 30% 1h30m · wk 54% · 45m ago\n"  # the weekly part is still the cache's
    stdin({"rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": epoch(T0 + 1.5 * H)},
                           "seven_day": {"used_percentage": 55, "resets_at": epoch(WEEK_RESET)}}})
    assert line(cli) == "5h 30% 1h30m · wk 55%\n"


def test_stdin_window_whose_reset_passed_is_ignored(clock, cli, stdin, no_backend):
    stdin({"rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": epoch(T0 - H)},
                           "seven_day": {"used_percentage": 10}}})
    assert line(cli) == "wk 10%\n"


def test_window_that_already_reset_counts_as_zero_and_drops_its_points(clock, cli, stdin, no_backend):
    cache(clock, session_pct=97.0, session_reset=T0 - 0.5 * H)
    sessions({"session": T0 - 0.5 * H, "weekly_all": WEEK_RESET},
             A={"cost_usd": 2.0, "pp": {"session": 30.0, "weekly_all": 3.0}})
    stdin({"session_id": "A"})
    assert line(cli) == "5h 0% · wk 54% · session $2.00 (wk 3pp)\n"
    stdin({"session_id": "A", "rate_limits": {
        "five_hour": {"used_percentage": 4, "resets_at": epoch(T0 + 4.5 * H)}}})
    assert line(cli) == "5h 4% 4h30m · wk 54% · session $2.00 (wk 3pp)\n"


def test_points_from_another_cycle_are_dropped(clock, cli, stdin, no_backend):
    cache(clock)
    sessions({"session": SESSION_RESET - 5 * H, "weekly_all": WEEK_RESET + 30 * timedelta(minutes=1)},
             A={"cost_usd": 2.0, "pp": {"session": 30.0, "weekly_all": 3.0}})
    stdin({"session_id": "A"})
    # 5h: the previous cycle's points; weekly: same cycle, its reset moved within the jitter.
    assert line(cli) == "5h 20% 2h0m · wk 54% · session $2.00 (wk 3pp)\n"


@pytest.mark.parametrize("content", [
    "{", "[]", '{"sessions": []}', '{"sessions": {"A": "x"}}',
    '{"windows": [], "sessions": {"A": {"cost_usd": 1, "pp": {"session": 1}}}}',
    '{"windows": {}, "sessions": {"A": {"cost_usd": "1", "pp": {"session": "1"}}}}',
])
def test_malformed_session_cache_is_ignored(clock, cli, stdin, no_backend, content):
    cache(clock)
    os.makedirs(os.path.dirname(cmon._sessions_cache()), exist_ok=True)
    with open(cmon._sessions_cache(), "w", encoding="utf-8") as f:
        f.write(content)
    stdin({"session_id": "A"})
    assert line(cli) == "5h 20% 2h0m · wk 54%\n"


def test_unexpected_error_still_prints_one_line_and_exits_0(clock, cli, stdin, monkeypatch):
    monkeypatch.setattr(cmon, "_read_cache", lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    result = cli("statusline")
    assert result.as_dict() == {"exit_code": 0, "stdout": "cmon error\n", "stderr": "cmon: statusline: boom\n"}


def test_cache_hit_is_fast(clock, cli, stdin, no_backend):
    cache(clock)
    sessions(**{f"s{i}": {"cost_usd": float(i), "pp": {"session": 0.1}} for i in range(cmon.SESSIONS_KEEP)})
    stdin({"session_id": "s7", "cost": {"total_cost_usd": 1.0}})
    started = time.perf_counter()
    out = line(cli)
    assert time.perf_counter() - started < 0.1
    assert out.endswith("session $1.00 (5h 0.1pp)\n")


def test_fresh_process_never_imports_duckdb_or_requests(home, tmp_path):
    """A statusLine command is a new process per redraw: prove the whole run stays off DuckDB
    (no lock wait) and off the network stack."""
    reset = datetime.now(UTC) + 2 * H + timedelta(minutes=1)
    cmon._write_cache([("session", "Current session", 42.0, reset.isoformat(), True)], datetime.now(UTC))
    code = (f"import sys; sys.path.insert(0, {ROOT!r}); import cmon; sys.argv = ['cmon', 'statusline']; "
            "cmon.main(); print(sorted(m for m in sys.modules if m.split('.')[0] in ('duckdb', 'requests')), "
            "file=sys.stderr)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CMON_")}
    env.update(HOME=str(home), CMON_DB=str(tmp_path / "usage.duckdb"))
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True,
                         input=json.dumps({"session_id": "x", "cost": {"total_cost_usd": 1.5}}), timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "5h 42% 2h0m · session $1.50\n"
    assert out.stderr.strip() == "[]"
    assert not (tmp_path / "usage.duckdb").exists()


def test_help(cli):
    result = cli("statusline", "--help")
    assert result.exit_code == 0 and result.stdout.startswith("usage: cmon statusline")
