from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cmon


def _seed_history() -> None:
    con = cmon.db()
    start = datetime(2030, 1, 1, tzinfo=UTC)
    legacy_rows = [
        (start, "session", "Current session", 20.0, start + timedelta(hours=5), True),
        (start, "weekly_all", "All models", 10.0, start + timedelta(days=7), True),
        (start + timedelta(hours=1), "session", "Current session", 30.0, start + timedelta(hours=5), True),
        (start + timedelta(hours=1), "weekly_all", "All models", 15.0, start + timedelta(days=7), True),
        (start + timedelta(hours=2), "session", "Current session", 5.0, start + timedelta(hours=7), True),
        (start + timedelta(hours=2), "weekly_all", "All models", 20.0, start + timedelta(days=7), True),
    ]
    rows = []
    for ordinal, (observed_at, key, label, percent, resets_at, is_active) in enumerate(legacy_rows):
        role, window_minutes = ("5h", 300) if key == "session" else ("weekly", 10080)
        rows.append(("claude", observed_at, key, role, label, percent, window_minutes,
                     resets_at, is_active, f"baseline-snapshot-{ordinal}", None))
    con.executemany(
        f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.close()


def _seed_forecasts() -> None:
    con = cmon.db()
    start = datetime(2030, 1, 1, tzinfo=UTC)
    legacy_rows = [
        (start, "session", 1.0, start + timedelta(hours=1), 40.0, 30.0, 10.0, "learned", 42.0, start),
        (start, "session", 3.0, start + timedelta(hours=3), 50.0, 30.0, 8.0, "linear", 47.0, start),
        (start, "session", 5.0, start + timedelta(hours=5), 55.0, 30.0, 5.0, "linear", 60.0, start),
    ]
    rows = [
        ("claude", f"baseline-forecast-{ordinal}", observed_at, observed_at, key, horizon_h, target_ts,
         pred_pct, pct0, rate, model, actual_pct, resolved_ts)
        for ordinal, (observed_at, key, horizon_h, target_ts, pred_pct, pct0, rate, model, actual_pct,
                      resolved_ts) in enumerate(legacy_rows)
    ]
    con.executemany(
        "INSERT INTO forecasts(provider,source_event_id,observed_at,ingested_at,key,horizon_h,target_ts,"
        "pred_pct,pct0,rate,model,actual_pct,resolved_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.close()


def test_now_text_baseline(cli, clock, fixture_json, monkeypatch, golden) -> None:
    monkeypatch.setattr(cmon, "fetch", lambda retries=cmon.RETRIES: fixture_json("claude-usage.json"))
    golden("now", cli("now"))


def test_status_text_baseline(cli, clock, fixture_json, golden) -> None:
    rows = cmon.limits(fixture_json("claude-usage.json"))
    cmon._write_cache(rows, cmon.datetime.now(UTC))
    golden("status", cli("status"))


@pytest.mark.parametrize(
    ("name", "args", "json_value"),
    [
        ("trends", ("trends",), None),
        (
            "trends_json",
            ("trends", "--json"),
            [
                {"label": "All models", "snapshots": 3, "peak_pct": 20, "consumed_pct": 10},
                {"label": "Current session", "snapshots": 3, "peak_pct": 30, "consumed_pct": 10},
            ],
        ),
    ],
)
def test_trends_baseline(cli, clock, golden, name: str, args: tuple[str, ...], json_value) -> None:
    _seed_history()
    result = cli(*args)
    golden(name, result)
    if json_value is not None:
        assert json.loads(result.stdout) == json_value


@pytest.mark.parametrize(
    ("name", "args", "json_value"),
    [
        ("burn", ("burn", "--since", "all"), None),
        (
            "burn_json",
            ("burn", "--since", "all", "--json"),
            [
                {"grp": "claude-sonnet-fixture", "tok": 3_150_000, "custo": 5.4},
                {"grp": "claude-opus-fixture", "tok": 1_570_000, "custo": 4.45},
            ],
        ),
    ],
)
def test_burn_baseline(cli, clock, install_claude_log, golden, name: str, args: tuple[str, ...], json_value) -> None:
    install_claude_log()
    result = cli(*args)
    golden(name, result)
    if json_value is not None:
        assert json.loads(result.stdout) == json_value


@pytest.mark.parametrize(
    ("name", "args", "json_value"),
    [
        ("backtest", ("backtest",), None),
        (
            "backtest_json",
            ("backtest", "--json"),
            [
                {"model": "learned", "horizon": "<=1h", "n": 1, "mae": 2.0, "bias": -2.0},
                {"model": "linear", "horizon": "1-6h", "n": 2, "mae": 4.0, "bias": -1.0},
            ],
        ),
    ],
)
def test_backtest_baseline(cli, clock, golden, name: str, args: tuple[str, ...], json_value) -> None:
    _seed_forecasts()
    result = cli(*args)
    golden(name, result)
    if json_value is not None:
        assert json.loads(result.stdout) == json_value


def test_tune_show_baseline(cli, clock, golden) -> None:
    con = cmon.db()
    con.close()
    golden("tune_show", cli("tune", "--show"))


def test_nonzero_exit_baseline(cli, clock, golden) -> None:
    golden("trends_no_history", cli("trends"))


@pytest.mark.parametrize(
    "command",
    [
        "now",
        "status",
        "trends",
        "burn",
        "collect",
        "watch",
        "wait",
        "backtest",
        "tune",
        "install",
        "uninstall",
        "plot",
        "token",
        "config",
    ],
)
def test_every_command_help_is_parseable(cli, command: str) -> None:
    result = cli(command, "--help")
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.startswith(f"usage: cmon {command}")


def test_parser_rejects_unknown_command_before_io(cli) -> None:
    result = cli("not-a-command")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "invalid choice: 'not-a-command'" in result.stderr


def test_fixture_directory_contains_only_synthetic_paths() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    contents = "\n".join(path.read_text(encoding="utf-8") for path in fixtures.iterdir())
    assert "/Users/" not in contents
    assert "/home/" not in contents
    assert "sk-" not in contents
    assert "Bearer " not in contents
