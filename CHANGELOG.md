# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Per-window time-based alerts**: `collect --alert` now fires a desktop notification when the
  **weekly** window nears its reset too, not just the 5h window. New `CMON_ALERT_LEAD_WEEKLY`
  (default 180min) sets the weekly lead; `CMON_ALERT_LEAD` still governs the 5h session (default
  60min). Each window deduplicates on its own reset, so both fire once per cycle, independently.

### Changed

- **`cmon watch` default poll interval** raised 30s → 45s (fewer requests, lighter background use).

## [0.1.2] — 2026-07-05

Security hardening (secperf audit), Cloudflare-403 resilience, and a smarter projection.

### Added

- **`cmon watch` adaptive backoff**: the poll interval self-tunes to survive Cloudflare
  bot-detection (403) — ×1.8 on any 403/error (cap 5min), eases back ×/1.5 after 5 clean
  reads, with ±15% jitter to break the fixed robotic cadence. The learned-safe interval is
  persisted (new `meta` table) and reused on the next `watch` within 2h.

### Changed

- **EWMA-weighted projection rate**: `_rate` now computes an exponentially-weighted
  least-squares slope over the current window (half-life ~1h for the 5h window, ~12h weekly)
  instead of a flat cycle average — recent snapshots dominate, so the projection tracks your
  current pace. `now` drops its duplicated inline rate calc and shares this one source with
  `watch`/`--advice`/alerts.

### Security

- **F1 — OAuth token exfil via stray `.env` closed**: `load_dotenv` scoped to cwd (no
  ancestor-dir walk); token-bearing HTTP calls use a `trust_env=False` session (no ambient
  proxy MITM); `CMON_OAUTH_TOKEN_URL` host-allowlisted to `*.anthropic.com`.
- **F2 — cleartext keyring refused**: `token set` aborts on a plaintext/null/fail backend
  (override `CMON_ALLOW_PLAINTEXT_KEYRING=1`) and auto-refresh won't persist the chain there.
- **F3 — `Retry-After` clamped** to [0,60]s so a hostile/garbage value can't hang the CLI.
- **F4 — log-derived labels sanitized** (control/ANSI chars stripped at the parse boundary)
  and JSONL lines > 4 MiB skipped (parser DoS guard).
- **F5 — Windows `schtasks` install** rejects an argument containing `"` instead of emitting
  a broken quoted command.

### Fixed

- **OAuth self-heal on a dead refresh chain**: when cmon's own refresh chain can't renew (its
  `refresh_token` was rotated/revoked after a Claude Code re-login), token resolution and the
  401 force-refresh fall back to Claude Code's fresh credential and re-seed the chain, instead
  of stranding on the expired access token — the persistent `401` that "Open Claude Code"
  could not clear.
- **`burn`/model-mix scoped to the current 5h window**: `watch`'s *burn this 5h window* line
  and `now --advice`'s model mix anchored to the session reset (`resets_at − 5h … now`) instead
  of a rolling `now − 5h`, so right after a reset they no longer count spend from the previous
  cycle.

### Performance

- **Vectorized reset detection** in `_rate`/`_cycles` (`df.percent.diff()<0` instead of an
  O(n) Python loop with scalar `.iloc`).
- **History-query scale hardening**: `--since` pushed into the `deltas()` SQL (F8), `token_log`
  inserted `ORDER BY ts` for zonemap pruning (F9), and `now` defers the DuckDB import until the
  projection is actually needed (F10).

## [0.1.0]

First development line. Everything below predates the 0.1.2 hardening pass.

### Added

- **CLI `cmon`** to track Claude plan consumption over time:
  commands `now`, `collect`, `trends`, and `plot`, reading `limits[]` from
  `claude.ai/api/oauth/usage` and writing snapshots to DuckDB.
- **Cross-platform token vault** via keyring (Keychain / Credential Manager /
  Secret Service), with resolution `env -> OS vault -> Claude Code credential`
  and commands `cmon token set/status/clear`.
- **OAuth token auto-refresh**: proactive renewal (reads `expiresAt`, 60s grace period)
  and reactive (on 401), with the refresh chain stored in its own vault, separate from
  Claude Code credential. `client_id`/endpoint configurable via env.
- **`cmon now --advice`**: window-based projection, %/h target, and tips generated via
  `claude -p` (Sonnet), grounded in the actual model mix from the last 5h.
- **`cmon trends`** folds in the former `report`: a per-label summary (`--since`, `--json`)
  followed by the reset-aware per-cycle breakdown with anomaly detection.
- **Time-based 5h alert**: `collect --alert` also warns when the 5h window resets within
  `CMON_ALERT_LEAD` minutes (default 60), once per cycle, via stderr + native notification
  and an optional **`CMON_HOOK`** command (message in `$CMON_ALERT_MSG`).
- **`cmon watch`**: live TUI (rich) with colored bars, burn rate, projection,
  alerts, and the `burn 5h (logs)` line; records each read (deduped) while watching.
- **Unified persistence**: every command that queries the API (`now`, `tips`, `watch`,
  `wait`) records its reading to DuckDB (deduped, best-effort — skipped if the
  single-writer DB is busy), so history accrues from normal use, not only scheduled
  `collect`. Read paths degrade gracefully instead of crashing when the DB is locked.
- **`cmon status`**: single line for statusline/tmux/prompt, gracefully degrading
  when offline.
- **`cmon wait`**: blocks until the window resets (or `--at N%`) and fires
  native notification.
- **`cmon trends`**: segments history by reset, with peak per cycle, delta
  vs. previous, and anomaly detection.
- **`cmon install/uninstall`**: background collection via launchd (macOS) /
  systemd-user with fallback cron (Linux) / schtasks (Windows); `--dry-run`.
- **`cmon burn`**: mines local Claude Code logs
  (`~/.claude/projects/**/*.jsonl`) to estimate tokens and US$, with breakdown by
  component (input/output/cache read/cache write), honest label
  ("API equivalent", not billed) and grouping by `model`, `day`, `project`,
  `session`, or `surface` (entrypoint: terminal/vscode/app/sdk).
- **Alerts**: warning when, at the current burn rate, the window hits 100% before reset
  (`_notify` best-effort via osascript/notify-send).
- **Robustness**: retry with exponential backoff respecting `Retry-After` for
  429/5xx/network; 401/403 fail with readable message; `collect` with dedup per
  window and fail-fast (exit code != 0).
- **Open-source packaging**: PyPI metadata, `LICENSE` (MIT), CI workflow
  (ruff + smoke on 3.11–3.13), graphs as optional extra (`plot`).

### Performance

- **`burn` first-scan ~55x faster**: vectorized insert via DataFrame +
  `drop_duplicates` instead of `executemany`+`ON CONFLICT`; scan with orjson,
  binary read, pre-filter by `"usage"`, and parallel parse (ProcessPool).
  Scan of 520MB/1939 files: 72s -> 1.3s; incremental 1.1s -> 0.28s.
- **`status` ~12x faster**: reads local cache (`~/.cmon/status.json`) before
  database and API, removing network from the hot path of statusline (~50ms vs. ~590ms).

### Changed

- `requires-python` relaxed from `>=3.14` to `>=3.11` (the code only needs
  3.11); `.python-version` on 3.12.
- PyPI distribution name is now `cmon-cli` (the terminal command remains `cmon`),
  since `cmon` was already taken.
- `burn` now uses a default window of 30 days (Claude Code removes transcripts
  older than 30d); `--since all` scans the entire available history.
