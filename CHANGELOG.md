# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

First development line toward `0.1.0`. No release published yet.

### Added

- **CLI `cmon`** to track Claude plan consumption over time:
  commands `now`, `collect`, `report`, and `plot`, reading `limits[]` from
  `claude.ai/api/oauth/usage` and writing snapshots to DuckDB.
- **Cross-platform token vault** via keyring (Keychain / Credential Manager /
  Secret Service), with resolution `env -> OS vault -> Claude Code credential`
  and commands `cmon token set/status/clear`.
- **OAuth token auto-refresh**: proactive renewal (reads `expiresAt`, 60s grace period)
  and reactive (on 401), with the refresh chain stored in its own vault, separate from
  Claude Code credential. `client_id`/endpoint configurable via env.
- **`cmon tips`**: window-based projection, %/h target, and tips generated via
  `claude -p` (Sonnet), grounded in the actual model mix from the last 5h.
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
