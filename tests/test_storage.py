import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

import cmon

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


def _usage_limit(**changes):
    values = {
        "provider": "codex",
        "key": "primary",
        "role": cmon.WindowRole.FIVE_HOUR,
        "label": "5h",
        "percent": 12.5,
        "window_minutes": 300,
        "observed_at": NOW,
        "ingested_at": NOW + timedelta(seconds=1),
        "source_event_id": "event-1",
        "resets_at": NOW + timedelta(hours=5),
    }
    values.update(changes)
    return cmon.UsageLimit(**values)


def _token_usage(**changes):
    values = {
        "provider": "codex",
        "source_event_id": "turn-1-v2",
        "observed_at": NOW,
        "ingested_at": NOW + timedelta(seconds=1),
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 40,
        "reasoning_tokens": 5,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
    }
    values.update(changes)
    return cmon.TokenUsage(**values)


@pytest.mark.parametrize("percent", [float("nan"), float("inf"), float("-inf"), -0.1, 100.1, True])
def test_usage_limit_rejects_invalid_percent(percent):
    with pytest.raises(ValueError):
        _usage_limit(percent=percent)


@pytest.mark.parametrize("field,value", [
    ("input_tokens", -1),
    ("output_tokens", True),
    ("cache_read_tokens", -1),
    ("cache_write_tokens", -1),
    ("reasoning_tokens", -1),
])
def test_token_usage_rejects_invalid_counts(field, value):
    with pytest.raises(ValueError):
        _token_usage(**{field: value})


def test_contracts_keep_provider_key_and_token_buckets_separate():
    with pytest.raises(ValueError, match="provider must"):
        _usage_limit(provider="other")
    with pytest.raises(ValueError, match="provider-local"):
        _usage_limit(key="codex:primary")
    with pytest.raises(ValueError, match="subset"):
        _token_usage(output_tokens=4, reasoning_tokens=5)
    usage = _token_usage()
    assert usage.input_total == 80
    assert usage.total_tokens == 100
    assert usage.total_tokens != 105  # reasoning is already included in output


def test_contracts_require_aware_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        _usage_limit(observed_at=datetime(2026, 9, 18, 12))
    with pytest.raises(ValueError, match="timezone-aware"):
        _token_usage(ingested_at=datetime(2026, 9, 18, 12))


def test_resolve_providers_is_pure_capability_aware_and_stable():
    available = {
        "codex": {cmon.ProviderCapability.QUOTA, cmon.ProviderCapability.TOKENS},
        "claude": {"quota", "history"},
    }
    assert cmon.resolve_providers("all", "quota", available) == ("claude", "codex")
    assert cmon.resolve_providers("all", "tokens", available) == ("codex",)
    assert cmon.resolve_providers("claude", "history", available) == ("claude",)
    with pytest.raises(cmon.ProviderUnavailable):
        cmon.resolve_providers("claude", "tokens", available)
    with pytest.raises(cmon.ProviderUnavailable):
        cmon.resolve_providers("all", "tokens", {"claude": {"quota"}})
    with pytest.raises(ValueError, match="invalid provider"):
        cmon.resolve_providers("other", "quota", available)


def test_new_schema_is_versioned_idempotent_and_multi_provider(tmp_path, monkeypatch):
    path = tmp_path / "new.duckdb"
    monkeypatch.setattr(cmon, "DB", str(path))
    con = cmon.db()
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (str(cmon.SCHEMA_VERSION),)
    columns = {row[1] for row in con.execute("PRAGMA table_info('scanned')").fetchall()}
    assert {"provider", "path", "cursor", "file_identity", "fingerprint", "size", "mtime"} <= columns
    turn_columns = {row[1] for row in con.execute("PRAGMA table_info('codex_turns')").fetchall()}
    assert {"thread_id", "turn_id", "reasoning_effort", "source_version", "reasoning_tokens"} <= turn_columns
    row = [NOW, "primary", "5h", "Five hours", 10.0, 300, NOW + timedelta(hours=5), True,
           "same-event", None]
    for provider in ("claude", "codex"):
        con.execute(
            f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [provider, *row],
        )
    con.execute(
        f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ["codex", NOW, "secondary", "weekly", "Weekly", 20.0, 10080, None, True,
         "same-event", None],
    )
    assert con.execute("SELECT count(*) FROM snapshots").fetchone() == (3,)
    indexes = con.execute("SELECT table_name FROM duckdb_constraints() WHERE constraint_type IN "
                          "('UNIQUE','PRIMARY KEY') AND table_name IN ('snapshots','token_log')").fetchall()
    assert indexes == []  # they took more space than the data; see _create_token_log
    con.close()
    con = cmon.db()
    assert con.execute("SELECT count(*) FROM snapshots").fetchone() == (3,)
    con.close()


def _create_legacy_database(path):
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE snapshots(ts TIMESTAMPTZ,key TEXT,label TEXT,percent DOUBLE,"
        "resets_at TIMESTAMPTZ,is_active BOOL)"
    )
    for percent in (10.0, 10.0):
        con.execute(
            "INSERT INTO snapshots(ts,key,label,percent,resets_at,is_active) VALUES (?,?,?,?,?,?)",
            [NOW, "session", "Current session", percent, NOW + timedelta(hours=5), True],
        )
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT,ts TEXT)")
    con.execute(
        "INSERT INTO meta(key,value,ts) VALUES (?,?,?)",
        ["model_params", '{"blend":0.5}', NOW.isoformat()],
    )
    con.execute(
        "CREATE TABLE forecasts(ts TIMESTAMPTZ,key TEXT,horizon_h DOUBLE,target_ts TIMESTAMPTZ,"
        "pred_pct DOUBLE,pct0 DOUBLE,rate DOUBLE,model TEXT,actual_pct DOUBLE,resolved_ts TIMESTAMPTZ)"
    )
    for _ in range(2):
        con.execute(
            "INSERT INTO forecasts(ts,key,horizon_h,target_ts,pred_pct,pct0,rate,model,actual_pct,resolved_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [NOW, "session", 1.0, NOW + timedelta(hours=1), 20.0, 10.0, 10.0, "linear", None, None],
        )
    # Oldest supported token cache: no entrypoint column.
    con.execute(
        "CREATE TABLE token_log(uuid TEXT PRIMARY KEY,ts TIMESTAMPTZ,model TEXT,project TEXT,session TEXT,"
        "in_tok BIGINT,out_tok BIGINT,cache_read BIGINT,cache_create BIGINT)"
    )
    con.execute(
        "INSERT INTO token_log(uuid,ts,model,project,session,in_tok,out_tok,cache_read,cache_create) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ["uuid-1", NOW, "sonnet", "project", "session-1", 1, 2, 3, 4],
    )
    con.execute("CREATE TABLE scanned(path TEXT PRIMARY KEY,mtime DOUBLE,size BIGINT)")
    con.execute("INSERT INTO scanned(path,mtime,size) VALUES (?,?,?)", ["/logs/a.jsonl", 1.5, 123])
    con.close()


def test_legacy_migration_preserves_counts_values_and_namespaces(tmp_path, monkeypatch):
    path = tmp_path / "legacy.duckdb"
    _create_legacy_database(path)
    monkeypatch.setattr(cmon, "DB", str(path))
    con = cmon.db()
    assert con.execute("SELECT count(*) FROM snapshots").fetchone() == (2,)
    assert con.execute("SELECT count(*) FROM forecasts").fetchone() == (2,)
    assert con.execute(
        "SELECT provider,session_id,source_event_id,surface,input_tokens,output_tokens,reasoning_tokens,"
        "cache_read_tokens,cache_write_tokens FROM token_log"
    ).fetchone() == ("claude", "session-1", "uuid-1", "?", 1, 2, 0, 3, 4)
    assert con.execute("SELECT provider,path,cursor,size FROM scanned").fetchone() == (
        "claude", "/logs/a.jsonl", 123, 123,
    )
    assert con.execute("SELECT value FROM meta WHERE key='claude:model_params'").fetchone() == ('{"blend":0.5}',)
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (str(cmon.SCHEMA_VERSION),)
    assert con.execute("SELECT DISTINCT provider,role,window_minutes FROM snapshots").fetchone() == (
        "claude", "5h", 300,
    )
    con.close()


V1_SNAPSHOTS = ("CREATE TABLE snapshots(provider TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL, "
                "ingested_at TIMESTAMPTZ NOT NULL, key TEXT NOT NULL, role TEXT NOT NULL, label TEXT NOT NULL, "
                "percent DOUBLE NOT NULL, window_minutes INTEGER NOT NULL, resets_at TIMESTAMPTZ, is_active BOOL, "
                "source_event_id TEXT NOT NULL, metadata_json TEXT, UNIQUE(provider, source_event_id, key))")
V1_TOKENS = ("CREATE TABLE token_log(provider TEXT NOT NULL, source_event_id TEXT NOT NULL, "
             "observed_at TIMESTAMPTZ NOT NULL, ingested_at TIMESTAMPTZ NOT NULL, thread_id TEXT NOT NULL, "
             "turn_id TEXT NOT NULL, model TEXT NOT NULL, surface TEXT NOT NULL, project TEXT NOT NULL, "
             "session_id TEXT NOT NULL, input_tokens BIGINT NOT NULL, output_tokens BIGINT NOT NULL, "
             "reasoning_tokens BIGINT NOT NULL, cache_read_tokens BIGINT NOT NULL, cache_write_tokens BIGINT NOT NULL, "
             "source_version BIGINT NOT NULL, metadata_json TEXT, PRIMARY KEY(provider, thread_id, turn_id), "
             "UNIQUE(provider, source_event_id))")


def _create_v1_database(path, rows=3000):
    con = duckdb.connect(str(path))
    con.execute(V1_SNAPSHOTS)
    con.execute(V1_TOKENS)
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT, ts TEXT)")
    con.execute("CREATE TABLE session_account(session TEXT PRIMARY KEY, account TEXT)")
    con.execute("CREATE VIEW token_terminal AS SELECT * FROM token_log WHERE provider = 'claude'")
    con.execute("INSERT INTO meta VALUES ('schema_version', '1', NULL), ('terminal_account', 'acct', NULL)")
    con.execute("INSERT INTO session_account VALUES ('s0', 'acct')")
    con.execute(
        "INSERT INTO snapshots SELECT 'claude', TIMESTAMPTZ '2026-01-01 00:00:00+00' + INTERVAL (i) MINUTE, now(), "
        "'session', '5h', 'Current session', (i % 100)::DOUBLE, 300, NULL, true, 'legacy-snapshot:' || md5(i::TEXT), "
        f"NULL FROM range({rows}) t(i)")
    con.execute(
        "INSERT INTO token_log SELECT 'claude', md5(i::TEXT) || ':' || md5((i + 1)::TEXT), "
        "TIMESTAMPTZ '2026-01-01 00:00:00+00' + INTERVAL (i) MINUTE, now(), 's' || (i % 7), "
        "md5(i::TEXT) || ':' || md5((i + 1)::TEXT), 'claude-opus', 'cli', 'proj', 's' || (i % 7), "
        "1, 2, 0, 3, 4, 0, NULL "
        f"FROM range({rows}) t(i)")
    con.close()


def test_v1_migrates_keeping_all_data_and_shrinks_the_file(tmp_path, monkeypatch):
    path = tmp_path / "v1.duckdb"
    _create_v1_database(path)
    before = path.stat().st_size
    monkeypatch.setattr(cmon, "DB", str(path))
    con = cmon.db()
    assert con.execute("SELECT count(*), sum(percent) FROM snapshots").fetchone() == (3000, 148500.0)
    assert con.execute("SELECT count(*), sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) "
                       "FROM token_log").fetchone() == (3000, 30000)
    assert con.execute("SELECT count(*) FROM token_terminal").fetchone() == (3000,)  # view rebuilt
    assert con.execute("SELECT value FROM meta WHERE key='terminal_account'").fetchone() == ("acct",)
    assert con.execute("SELECT count(*) FROM session_account").fetchone() == (1,)
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("2",)
    assert "ingested_at" not in {r[1] for r in con.execute("PRAGMA table_info('snapshots')").fetchall()}
    con.close()
    assert path.stat().st_size < before
    assert not (tmp_path / "v1.duckdb.compact").exists()
    assert path.stat().st_mode & 0o077 == 0
    con = cmon.db()  # idempotent: reopening neither migrates nor compacts again
    assert con.execute("SELECT count(*) FROM token_log").fetchone() == (3000,)
    con.close()


@pytest.mark.parametrize("phase", ["staged", "copied", "validated", "swapped", "versioned"])
def test_v1_migration_failure_rolls_back_and_can_retry(tmp_path, monkeypatch, phase):
    path = tmp_path / f"v1-{phase}.duckdb"
    _create_v1_database(path, rows=50)
    monkeypatch.setattr(cmon, "DB", str(path))

    def fail(current):
        if current == phase:
            raise RuntimeError(f"boom at {phase}")

    monkeypatch.setattr(cmon, "_MIGRATION_TEST_HOOK", fail)
    with pytest.raises(cmon.MigrationError, match="migration"):
        cmon.db()
    raw = duckdb.connect(str(path))
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("1",)
    assert raw.execute("SELECT count(*) FROM token_terminal").fetchone() == (50,)
    assert "ingested_at" in {r[1] for r in raw.execute("PRAGMA table_info('token_log')").fetchall()}
    raw.close()
    monkeypatch.setattr(cmon, "_MIGRATION_TEST_HOOK", None)
    con = cmon.db()
    assert con.execute("SELECT count(*) FROM token_log").fetchone() == (50,)
    con.close()


def test_migration_rejects_a_row_count_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "mismatch.duckdb"
    _create_legacy_database(path)
    monkeypatch.setattr(cmon, "DB", str(path))
    monkeypatch.setattr(cmon, "_migrate_token_log", lambda _con: None)  # loses every token row
    with pytest.raises(cmon.MigrationError, match="count mismatch"):
        cmon.db()
    raw = duckdb.connect(str(path))
    assert raw.execute("SELECT count(*) FROM token_log").fetchone() == (1,)
    raw.close()


def test_failed_compaction_keeps_the_database_working(tmp_path, monkeypatch):
    path = tmp_path / "v1.duckdb"
    _create_v1_database(path, rows=50)
    monkeypatch.setattr(cmon, "DB", str(path))
    real = cmon.os.replace
    monkeypatch.setattr(cmon.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("no swap")))
    con = cmon.db()
    assert con.execute("SELECT count(*) FROM token_log").fetchone() == (50,)
    con.close()
    monkeypatch.setattr(cmon.os, "replace", real)
    assert not (tmp_path / "v1.duckdb.compact").exists()


@pytest.mark.parametrize("phase", ["staged", "copied", "validated", "swapped", "versioned"])
def test_migration_failure_rolls_back_every_phase(tmp_path, monkeypatch, phase):
    path = tmp_path / f"rollback-{phase}.duckdb"
    _create_legacy_database(path)
    monkeypatch.setattr(cmon, "DB", str(path))

    def fail(current):
        if current == phase:
            raise RuntimeError(f"boom at {phase}")

    monkeypatch.setattr(cmon, "_MIGRATION_TEST_HOOK", fail)
    with pytest.raises(cmon.MigrationError, match="migration"):
        cmon.db()
    raw = duckdb.connect(str(path))
    assert {row[1] for row in raw.execute("PRAGMA table_info('snapshots')").fetchall()} == {
        "ts", "key", "label", "percent", "resets_at", "is_active",
    }
    assert raw.execute("SELECT count(*) FROM snapshots").fetchone() == (2,)
    assert raw.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '__legacy_%'"
    ).fetchone() == (0,)
    assert raw.execute("SELECT count(*) FROM meta WHERE key='schema_version'").fetchone() == (0,)
    raw.close()


def test_snapshot_does_not_swallow_migration_error(monkeypatch):
    monkeypatch.setattr(cmon, "fetch", lambda: {"limits": []})

    def fail_db(*_args, **_kwargs):
        raise cmon.MigrationError("broken migration")

    monkeypatch.setattr(cmon, "db", fail_db)
    with pytest.raises(cmon.MigrationError, match="broken migration"):
        cmon._snapshot()


def test_cache_v1_reads_as_claude_and_v2_merges_providers_and_windows(tmp_path, monkeypatch):
    cache = tmp_path / "status.json"
    monkeypatch.setattr(cmon, "CACHE", str(cache))
    legacy = {
        "ts": NOW.isoformat(),
        "rows": [
            ["session", "Current session", 10.0, None, True],
            ["weekly_all", "All models", 20.0, None, True],
        ],
    }
    cache.write_text(json.dumps(legacy), encoding="utf-8")
    assert {row[0] for row in cmon._read_cache()[0]} == {"session", "weekly_all"}
    cmon._write_cache([["primary", "5h", 30.0, None, True]], NOW + timedelta(minutes=1), "codex")
    assert cmon._read_cache("claude") is not None
    assert [row[0] for row in cmon._read_cache("codex")[0]] == ["primary"]
    cmon._write_cache([["session", "Current session", 11.0, None, True]], NOW + timedelta(minutes=2), partial=True)
    assert {row[0] for row in cmon._read_cache("claude")[0]} == {"session", "weekly_all"}
    cmon._write_cache([["session", "Current session", 12.0, None, True]], NOW + timedelta(minutes=3))
    assert {row[0] for row in cmon._read_cache("claude")[0]} == {"session"}  # retired window is gone
    assert [row[0] for row in cmon._read_cache("codex")[0]] == ["primary"]
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert set(payload["providers"]) == {"claude", "codex"}
    assert not list(tmp_path.glob(".status.*.tmp"))


def test_corrupt_cache_is_rejected_and_atomic_writer_recovers(tmp_path, monkeypatch):
    cache = tmp_path / "status.json"
    cache.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cmon, "CACHE", str(cache))
    assert cmon._read_cache() is None
    cmon._write_cache([["session", "Current session", 1.0, None, True]], NOW)
    assert cmon._read_cache() is not None
    assert json.loads(cache.read_text(encoding="utf-8"))["version"] == 2
    assert not list(tmp_path.glob(".status.*.tmp"))
