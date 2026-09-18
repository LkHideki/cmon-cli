CREATE TABLE snapshots(
    ts TIMESTAMPTZ,
    key TEXT,
    label TEXT,
    percent DOUBLE,
    resets_at TIMESTAMPTZ,
    is_active BOOL
);
CREATE TABLE token_log(
    uuid TEXT PRIMARY KEY,
    ts TIMESTAMPTZ,
    model TEXT,
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
    'weekly_all',
    'All models',
    7.0,
    '2030-01-08T00:00:00Z',
    true
);
INSERT INTO token_log VALUES (
    'fixture-pre-entrypoint-turn',
    '2030-01-01T00:02:00Z',
    'claude-haiku-fixture',
    'fixture-project',
    'fixture-session',
    80,
    10,
    40,
    5
);
INSERT INTO scanned VALUES ('/fixture/pre-entrypoint.jsonl', 1893456000.0, 256);
