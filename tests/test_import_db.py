import importlib
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import duckdb

import cmon


def test_default_db_is_fixed_under_home(monkeypatch):
    monkeypatch.delenv("CMON_DB", raising=False)
    try:
        assert importlib.reload(cmon).DB == cmon.os.path.expanduser("~/.cmon/usage.duckdb")
    finally:
        importlib.reload(cmon)


def _source(path, snaps, tokens=()):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE snapshots(ts TIMESTAMPTZ, key TEXT, label TEXT, percent DOUBLE, "
                "resets_at TIMESTAMPTZ, is_active BOOL)")
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", snaps)
    con.execute("CREATE TABLE token_log(uuid TEXT PRIMARY KEY, ts TIMESTAMPTZ, model TEXT, entrypoint TEXT, "
                "project TEXT, session TEXT, in_tok BIGINT, out_tok BIGINT, cache_read BIGINT, cache_create BIGINT)")
    con.executemany("INSERT INTO token_log VALUES (?,?,?,?,?,?,?,?,?,?)", tokens)
    con.close()


def test_import_merges_dedupes_and_is_idempotent(home):
    t0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
    snap = lambda dt, pct: (dt, "session", "Current session", pct, t0 + timedelta(hours=5), True)  # noqa: E731
    tok = lambda u: (u, t0, "claude-opus", "cli", "p", "s", 1, 2, 3, 4)  # noqa: E731
    a, b = home / "a.duckdb", home / "b.duckdb"
    _source(a, [snap(t0, 10), snap(t0 + timedelta(minutes=20), 12)], [tok("u1"), tok("u2")])
    # b overlaps a: one snapshot 10 s after a's (same reading, other collector) + one new; u2 shared
    _source(b, [snap(t0 + timedelta(seconds=10), 10), snap(t0 + timedelta(minutes=40), 15)],
            [tok("u2"), tok("u3")])

    cmon.import_db(Namespace(paths=[str(a), str(b)]))
    cmon.import_db(Namespace(paths=[str(a), str(b)]))  # second run adds nothing

    con = duckdb.connect(cmon.DB, read_only=True)
    assert [r[0] for r in con.execute("SELECT percent FROM snapshots ORDER BY observed_at").fetchall()] == [10, 12, 15]
    assert con.execute("SELECT count(*) FROM token_log").fetchone()[0] == 3
    con.close()
    assert duckdb.connect(str(a), read_only=True).execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2


def test_import_reads_a_current_schema_source(home):
    t0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
    src = home / "current.duckdb"
    cmon_db, cmon.DB = cmon.DB, str(src)
    con = cmon.db()
    con.executemany(f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("claude", t0, "session", "5h", "Current session", 10.0, 300, None, True, None, None)])
    con.execute(f"INSERT INTO token_log({cmon._TOKEN_LOG_COLS}) VALUES "
                "('claude','k:1',?,'m','cli','p','s',1,2,0,3,4,0,NULL)", [t0])
    con.close()
    cmon.DB = cmon_db

    cmon.import_db(Namespace(paths=[str(src)]))

    con = duckdb.connect(cmon.DB, read_only=True)
    assert con.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    assert con.execute("SELECT source_event_id FROM token_log").fetchone()[0] == "k:1"
    con.close()
