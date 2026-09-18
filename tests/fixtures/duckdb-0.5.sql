CREATE TABLE snapshots(
    ts TIMESTAMPTZ,
    key TEXT,
    label TEXT,
    percent DOUBLE,
    resets_at TIMESTAMPTZ,
    is_active BOOL
);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT, ts TEXT);
CREATE TABLE forecasts(
    ts TIMESTAMPTZ,
    key TEXT,
    horizon_h DOUBLE,
    target_ts TIMESTAMPTZ,
    pred_pct DOUBLE,
    pct0 DOUBLE,
    rate DOUBLE,
    model TEXT,
    actual_pct DOUBLE,
    resolved_ts TIMESTAMPTZ
);
CREATE TABLE token_log(
    uuid TEXT PRIMARY KEY,
    ts TIMESTAMPTZ,
    model TEXT,
    entrypoint TEXT,
    project TEXT,
    session TEXT,
    in_tok BIGINT,
    out_tok BIGINT,
    cache_read BIGINT,
    cache_create BIGINT
);
CREATE TABLE scanned(path TEXT PRIMARY KEY, mtime DOUBLE, size BIGINT);

INSERT INTO snapshots VALUES (
    '2030-01-01T00:00:00Z',
    'session',
    'Current session',
    12.0,
    '2030-01-01T05:00:00Z',
    true
);
INSERT INTO token_log VALUES (
    'fixture-legacy-turn-001',
    '2030-01-01T00:01:00Z',
    'claude-sonnet-fixture',
    'cli',
    'fixture-project',
    'fixture-session',
    100,
    20,
    50,
    10
);
INSERT INTO scanned VALUES ('/fixture/legacy.jsonl', 1893456000.0, 512);
