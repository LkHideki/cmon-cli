from __future__ import annotations

import concurrent.futures
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cmon

FIXTURES = Path(__file__).parent / "fixtures"


def test_clock_and_filesystem_are_isolated(clock, paths) -> None:
    assert cmon.datetime.now(UTC) == datetime(2030, 1, 2, 6, 0, tzinfo=UTC)
    clock.set("2030-01-03T07:30:00+00:00")
    assert cmon._parse_since("24h") == datetime(2030, 1, 2, 7, 30, tzinfo=UTC)
    assert Path(cmon.DB) == paths.db
    assert Path(cmon.CACHE) == paths.cache
    assert Path(cmon.CONFIG) == paths.config
    assert Path(cmon.LOGS_ROOT) == paths.logs


def test_network_is_blocked() -> None:
    with pytest.raises(AssertionError, match="must not use the network"):
        cmon._session()


def test_duckdb_is_temporary(paths) -> None:
    con = cmon.db()
    assert con.execute("SELECT count(*) FROM snapshots").fetchone() == (0,)
    con.close()
    assert paths.db.is_file()


@pytest.mark.parametrize(
    ("fixture", "expected_columns"),
    [
        (
            "duckdb-0.5.sql",
            [
                "uuid",
                "ts",
                "model",
                "entrypoint",
                "project",
                "session",
                "in_tok",
                "out_tok",
                "cache_read",
                "cache_create",
            ],
        ),
        (
            "duckdb-pre-0.5.sql",
            [
                "uuid",
                "ts",
                "model",
                "project",
                "session",
                "in_tok",
                "out_tok",
                "cache_read",
                "cache_create",
            ],
        ),
    ],
)
def test_legacy_duckdb_fixtures_bootstrap(paths, fixture: str, expected_columns: list[str]) -> None:
    import duckdb

    con = duckdb.connect(str(paths.root / fixture.replace(".sql", ".duckdb")))
    con.execute((FIXTURES / fixture).read_text(encoding="utf-8"))
    columns = [row[1] for row in con.execute("PRAGMA table_info('token_log')").fetchall()]
    assert columns == expected_columns
    assert con.execute("SELECT count(*) FROM snapshots").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM token_log").fetchone() == (1,)
    con.close()


@pytest.mark.parametrize("use_stdlib_json", [False, True])
def test_claude_parser_backends(
    install_claude_log, monkeypatch: pytest.MonkeyPatch, use_stdlib_json: bool
) -> None:
    path = install_claude_log()
    if use_stdlib_json:
        monkeypatch.setattr(cmon, "_loads", json.loads)
    rows = cmon._parse_jsonl(str(path))
    assert [row[0] for row in rows] == ["fixture-claude-turn-001", "fixture-claude-turn-002"]
    assert sum(row[6] + row[7] + row[8] + row[9] for row in rows) == 4_720_000


def test_parse_many_process_pool_workers_are_picklable(install_claude_log) -> None:
    source = install_claude_log()
    paths = []
    for index in range(8):
        target = source.with_name(f"parallel-{index}.jsonl")
        target.write_bytes(source.read_bytes())
        paths.append(str(target))
    chunks = cmon._parse_many(paths)
    assert len(chunks) == 8
    assert all(len(chunk) == 2 for chunk in chunks)


def test_parse_many_falls_back_to_serial(
    install_claude_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = install_claude_log()
    paths = [str(source)] * 8

    class BrokenPool:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("synthetic pool failure")

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", BrokenPool)
    chunks = cmon._parse_many(paths)
    assert len(chunks) == 8
    assert all(len(chunk) == 2 for chunk in chunks)
