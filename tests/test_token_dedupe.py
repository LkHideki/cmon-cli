import json
from datetime import UTC, datetime, timedelta

import cmon

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _line(uuid, mid, rid, session, at, usage, model="claude-opus-4-1", entry="cli"):
    return json.dumps({"uuid": uuid, "sessionId": session, "requestId": rid, "entrypoint": entry,
                       "timestamp": at.isoformat().replace("+00:00", "Z"), "cwd": "/w/proj",
                       "message": {"id": mid, "model": model, "usage": usage}})


USAGE = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300,
         "cache_creation_input_tokens": 40}


def _write(home, name, lines):
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(lines) + "\n")


def _totals(con):
    return con.execute("SELECT count(*), "
                       "sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) "
                       "FROM token_log").fetchone()


def test_one_row_per_api_message_across_lines_and_forked_transcripts(home):
    # one response streamed as thinking/text/tool_use lines, then copied into a resumed session
    msg = [_line(f"u{i}", "msg_A", "req_A", "s1", T0 + timedelta(seconds=i), USAGE) for i in range(3)]
    fork = [_line("u9", "msg_A", "req_A", "s2", T0 + timedelta(seconds=5), USAGE)]
    other = [_line("u5", "msg_B", "req_B", "s1", T0 + timedelta(minutes=1), USAGE)]
    _write(home, "s1.jsonl", msg + other)
    _write(home, "s2.jsonl", fork)
    con = cmon.db()
    cmon.scan_logs(con, quiet=True)
    assert _totals(con) == (2, 2 * 370)
    cmon.scan_logs(con, quiet=True)  # nothing changed: no-op
    assert _totals(con) == (2, 2 * 370)


def test_legacy_rows_migrate_exact_when_on_disk_and_heuristic_when_deleted(home):
    con = cmon.db()
    legacy = lambda uuid, at, out=20: ("claude", uuid, at, T0, "s0", uuid, "claude-opus-4-1", "cli", "proj",  # noqa: E731
                                       "s0", 10, out, 0, 300, 40, 0, None)
    con.executemany(f"INSERT INTO token_log({cmon._TOKEN_LOG_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        # transcript still on disk: three line-keyed rows of one message
        legacy("u0", T0), legacy("u1", T0 + timedelta(seconds=1)), legacy("u2", T0 + timedelta(seconds=2)),
        # transcript already deleted: two lines of one message + a distinct message an hour later
        legacy("d0", T0 - timedelta(days=9)), legacy("d1", T0 - timedelta(days=9, seconds=-3)),
        legacy("d2", T0 - timedelta(days=9, hours=-1)),
        # deleted, different usage within the window: a different message, kept
        legacy("d3", T0 - timedelta(days=9, seconds=-30), out=21),
    ])
    _write(home, "s0.jsonl", [_line(f"u{i}", "msg_A", "req_A", "s0", T0 + timedelta(seconds=i), USAGE)
                              for i in range(3)])
    cmon.scan_logs(con, quiet=True)
    keys = sorted(r[0] for r in con.execute("SELECT source_event_id FROM token_log").fetchall())
    assert keys == ["d0", "d2", "d3", "msg_A:req_A"]
    assert cmon._meta_get(con, cmon.TOKEN_KEY_META)[0] == "v2"
    pk = con.execute("SELECT count(*) FROM duckdb_constraints() WHERE table_name = 'token_log' "
                     "AND constraint_type = 'PRIMARY KEY'").fetchone()[0]
    assert pk == 1


def test_import_after_migration_triggers_rededupe(home, tmp_path):
    con = cmon.db()
    cmon.scan_logs(con, quiet=True)
    assert cmon._meta_get(con, cmon.TOKEN_KEY_META)[0] == "v2"
    con.close()
    src = tmp_path / "old.duckdb"
    import duckdb
    duckdb.connect(str(src)).close()
    from argparse import Namespace
    cmon.import_db(Namespace(paths=[str(src)]))
    assert cmon._meta_get(cmon.db(), cmon.TOKEN_KEY_META)[0] == ""
