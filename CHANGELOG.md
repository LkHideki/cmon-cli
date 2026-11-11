# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [0.8.0] — 2026-11-10

### Added

- **`cmon burn --plan` shows who spent the percent.** `cmon burn --by surface --plan` splits the
  window's official percent across clients (terminal, VS Code, desktop app, SDK) by the cost each
  logged while it rose, and reports what rose with nothing logged locally (claude.ai chat, other
  devices) as *other*. Works with any `--by`; defaults to the current cycle.
- **cmon Bar shows usage by client.** The menu gets a *By client* section: a stacked bar per
  window and a legend with each client's share and points of the limit. New title placeholders
  `{top_client}` and `{5h_top_client}`. The split comes from `cmon collect`, and `status --json`
  carries it as an optional `by_client` on each window, so older readers are unaffected.
- **`cmon burn --surface vscode`** narrows any report to some clients.
- **Usage by device.** `cmon config set sync_dir <folder>` and `cmon sync` exchange rows between
  machines through a folder they already sync, with no server; `collect` keeps it up to date.
  `cmon burn --by device` and `--device NAME` use it. Files from another Claude account are
  skipped. No database migration.
- **`cmon config set keepalive on` starts the next 5h window by itself.** When a window rolls over
  and sits parked at `0% –`, cmon sends one small Haiku request through the `claude` command. Off by
  default, because it sends requests on your behalf and they count towards your limits.
  `cmon keepalive` sends one on demand.

## [0.7.1] — 2026-11-03

### Upgrading

- The database migrates once more on first use, in under a second on a real history. 0.7.0 can't
  read the result, so copy `~/.cmon/usage.duckdb` first if you may go back. `make install` no
  longer keeps a copy, because it would bring the weight back.

### Changed

- **The database is about 87% smaller with all its data**: 103 MB to 14 MB on a real history of
  88k snapshots and 207k token rows. `snapshots` and `token_log` lost their unique indexes, which
  took more space than the data, plus columns that repeated others; inserts now dedupe with an
  anti-join, which takes about 11 ms on that history. After a migration the file is rewritten,
  because DuckDB never returns freed space to the disk.

### Fixed

- The migration's row count check never ran, because it compared names that could not match.
- The lock file now uses DuckDB 2.0.0, like an installed cmon: files it writes can't be opened by
  1.5.4, so `uv run cmon` from a clone couldn't read the database.

## [0.7.0] — 2026-11-03

### Upgrading

- History moves to a provider-aware schema on first use, in one transaction. **Older cmon versions
  can't read it**, so keep a copy if you may go back: `make install` makes one at
  `~/.cmon/usage.before-provider-schema.duckdb`; other installs should copy `~/.cmon/usage.duckdb`
  first. cmon still tracks Claude only: this prepares the database for Codex, which no command reads yet.
- A `bar.json` still holding the old default layout switches once to the new one. To go back,
  set `"segments": ["{5h}", "{wk}", "{reset}", "{wk_reset_h}"]` and `"separator": "•"`; it won't
  be changed again.

### Added

- **`make install`, `make uninstall`, `make doctor`**: one command sets up the `cmon` command,
  cmon Bar and background collection at user level, and says what to fix if something is off.
- **cmon Bar title in two groups** (`90% 75'  44% 91`): the 5h window and the weekly limit, each
  as percent used and time left (plain hours, `'` minutes, `"` seconds), through `{5h_pair}`
  `{wk_pair}`, with a legend in the menu and tooltip. `{5h_pct}` `{wk_pct}` `{reset_short}`
  `{wk_reset_short}` are there for other layouts.
- **[AGENTS.md](AGENTS.md)**, so an AI assistant can install and operate cmon for a user.
- **Labelled title layouts**: `hide_empty` in `bar.json` drops blank values with their
  decoration, and `{5h_bar}` `{wk_bar}` `{5h_dot}` `{wk_dot}` show usage as blocks or a traffic light.

### Changed

- DuckDB is now `>=1.1.1,!=1.3.0`: 1.1.0 crashes closing a just-migrated database, and 1.3.0 misses
  duplicates in `cmon import`.
- Design plans moved to `docs/plans/`.

### Fixed

- The clone URL in the docs pointed to a repository that doesn't exist.
- Under an hour from the weekly reset, the title showed `0`; it now shows minutes.
- On a Mac without the Command Line Tools, `menubar/install.sh` passed its check and then failed
  on the system's `swiftc` stub. A failed icon build no longer stops the install.
- A retired limit (a model-scoped weekly window) no longer stays in the status cache.

## [0.6.1] — 2026-10-03

### Added

- **cmon Bar shows the 5h countdown near the reset**, not only near the limit:
  `reset_within_min` (default 100) joins `reset_above_percent`, and the visible countdown
  now ticks every minute on its own instead of waiting for the next data refresh.

## [0.6.0] — 2026-10-03

### Upgrading

- History now lives in `~/.cmon/usage.duckdb`. If you ran 0.5 from several directories,
  merge the old databases once: `cmon import ~/usage.duckdb ~/Projects/*/usage.duckdb`.
- Token burn is re-counted per API message on the first scan (a one-off of a few seconds);
  expect totals about half to a third of what 0.5 reported.
- cmon no longer refreshes Claude Code's login. If Claude Code hasn't run for hours, its
  access token may be expired until it runs again; `cmon token status` shows the expiry.
- `.env` is read from `~/.cmon/.env` (not the current directory).
- cmon Bar: `refresh_seconds` and `hot_*` in `bar.json` are replaced by the `refresh` rules.

### Changed

- **The database now defaults to `~/.cmon/usage.duckdb`** instead of `./usage.duckdb`, which
  split history across every directory cmon ran in. `cmon import PATH...` merges old
  databases (read-only sources; overlapping snapshots deduplicated).

### Security

- **`.env` is no longer read from the current directory.** A project's `.env` could set
  `CMON_HOOK` (run with a shell on alerts), `CMON_DB`, or another account's token. Only
  `~/.cmon/.env` and the cmon checkout's own `.env` are read, before settings load (so
  tuning variables there now take effect); files writable by others are refused.
- **`claude -p` runs sandboxed** from an empty private directory, so an untrusted repo's
  `.claude/settings.json` hooks and `CLAUDE.md` don't apply to the AI tip; it and the alert
  hook no longer inherit `CLAUDE_OAUTH_TOKEN`.
- **`~/.cmon` is private (0700)** and a new database is created 0600 — it holds project
  names and session ids.
- **cmon no longer logs Claude Code out.** It used to redeem Claude Code's `refresh_token`
  and keep the rotated chain in its own vault entry; since refresh tokens rotate on use,
  this invalidated Claude Code's login. Claude Code's credential is now strictly read-only,
  an expired token is reported without any request, and the old `claude-oauth-auto` vault
  entry is deleted. `CMON_OAUTH_CLIENT_ID` / `CMON_OAUTH_TOKEN_URL` are gone.

### Fixed

- **Rate alerts fire once per window cycle.** `collect --alert` re-notified on every run
  (every 20 min by default) while the projection stayed above 100%; it now keeps the same
  per-cycle marker as the lead alert.
- **`wait` survives network blips.** Transient failures (network, 429/5xx, a Claude Code
  token awaiting renewal) back off exponentially up to 5 min and keep waiting, with one
  notice instead of exiting; only unrecoverable ones (no credential, 403) stop it.
- **A 401 retry no longer costs an attempt.** After a 401 cmon re-reads Claude Code's
  credential once without spending a retry, so `status` (one attempt) picks up a renewed
  token instead of reporting offline.
- **Token burn was counted 2–3× too high.** Claude Code writes one API response as several
  transcript lines (thinking, text, tool_use) that repeat the same usage, and copies lines
  into resumed sessions; rows are now keyed per API message (`message.id` + `requestId`).
  Existing history migrates once: rows from transcripts still on disk are re-read exactly,
  rows from deleted transcripts are deduplicated heuristically (~4% residual).
- **`watch` no longer locks out the scheduled `collect`.** It held DuckDB's single-writer
  connection for its whole session, so `collect --alert`, `burn`, `trends` and `plot` failed
  while it ran. Each tick now fetches first, then opens, writes and closes; `collect` waits
  up to 30 s for the lock, and `now --advice` releases it before the slow AI tip.
- **`status` can no longer hang or traceback.** The cache is written atomically (temp file +
  `os.replace`, mode 0600), so a concurrent reader never sees a torn file; the DuckDB
  fallback degrades when another cmon holds the lock; and an expired cache refetches with a
  5 s timeout, at most once every 2 min while offline (it used to block up to 30 s on every
  render). `config` and alert state use the same atomic write.
- **Log scans no longer stall on large batches.** Rows are bulk-loaded through a private
  temp CSV instead of list parameters (~100 µs/element in DuckDB); a full rescan of 1.5k
  transcripts went from ~100 s to ~4 s.

### Added

- **Per-account token accounting.** Claude Code sessions run from Claude Desktop under a
  different account no longer inflate burn, the hourly profile or `plot`; `burn` reports
  what it excluded and `--all-accounts` brings it back.
- **`cmon status --json`** — versioned, provider-grouped payload read from the local cache.
- **cmon Bar refresh schedule is a list of rules** (`refresh` in `~/.cmon/bar.json`):
  each rule sets `every_s` under optional `reset_within_min` / `usage_above_pct`
  conditions, the shortest applicable interval wins, and the bar queries the API itself
  on that schedule (default 10 min, tightening to 59 s in the last hour or above 87%).
  Replaces `refresh_seconds` and `hot_*`. The menu shows when the next update is due, and
  an invalid `bar.json` is reported instead of silently ignored.
- **cmon Bar live countdowns.** In the last minute durations count in seconds ("Resets in
  42s", "Updated 12s ago"); the open menu and the final minute redraw at 1 Hz, and the new
  cycle is fetched as soon as the 5h window rolls over.
- **cmon Bar hot mode and icon.** Above 87% of the 5h window or within its last hour the
  bar polls the API every 59 s; a watchdog kills a wedged `cmon` child, its stderr shows in
  the menu, and the timer keeps running while the menu is open. `install.sh` builds aside
  before replacing the running app and draws the app icon (a two-ring quota gauge).
- **cmon Bar** (`menubar/`) — native macOS menu bar item with a configurable compact title
  and a per-window breakdown; installed with `menubar/install.sh`.

## [0.5.0] — 2026-08-30

### Fixed

- **`cmon watch` no longer starts on a blank terminal.** The first render ran the whole
  pipeline — network fetch (30s timeout + retry; measured 43.8s on a slow night), transcript
  scan, forecasts — before the TUI ever painted. The first frame now comes instantly from the
  local snapshot cache when it's under 10 minutes old (marked *cached Ns ago · updating…*),
  or from a *connecting…* panel; the fetch happens inside the loop.

### Changed

- **Succinct `watch` footer lines** for narrow terminals: *projection: profile-weighted*,
  *burn 5h (logs): … · $ …*, alerts without the rate (it's already in the table), status line
  without timezone/"every"/"Ctrl-".
- Model mix shortens Fable/Mythos ids (*Fable 100%* instead of *claude-fable-5 100%*), in
  `watch` and `burn`.

## [0.4.0] — 2026-08-30

### Changed

- **`cmon watch` hides the Usage bar column by default** — the compact table keeps Window, %,
  resets in, rate, and projection, with the green/yellow/red cue moved onto the % cell.
  `-u/--usage` restores the bar chart.

## [0.3.0] — 2026-08-30

### Changed

- **Rarely-used hours weigh less in the projection.** The hourly shape was a recency-weighted
  mean, so one heavy 4am among weeks of idle nights left every future 4am carrying burst/n
  intensity — the projection kept spending the madrugada at a rate that almost never happens.
  Each hour's estimate is now multiplied by `occupancy^robust`, where occupancy is the
  recency-weighted share of days with real activity in that hour (above 5% of the mean, so a
  stray heartbeat request doesn't count) and `robust` is a fourth tunable parameter
  (default 0.5, grid 0/0.5/1) fitted by `cmon tune` like the others. On real data 01h drops
  0.24x→0.14x of the mean and 02–05h to ~0.03x, while habitual daytime hours rise ~9% through
  the anchor renormalization; replay MAE is unchanged in aggregate (≤1h improves 3.25→3.12).
  Applies to the `token_log` source only — the snapshot fallback can't observe occupancy — and
  the profile's returned mean is recomputed from the damped shape so `_project`'s 0.25×mean
  anchor floor keeps its meaning.

## [0.2.0] — 2026-08-17

### Added

- **`cmon backtest`**: scores past projections against what actually happened — MAE and bias per
  horizon band, for the active model *and* the linear baseline. Predictions are logged to a new
  `forecasts` table and settled against the nearest snapshot once their horizon passes (pairs
  whose window reset in between are dropped, since the drop isn't a forecasting error). Deduped
  to one write per 15min and pruned after 60 days.
- **`cmon backtest --replay`**: scores the history you already have instead of waiting weeks for
  live forecasts to mature. Each past prediction is rebuilt from the data available *at that
  moment* — `_rate` and `_hour_profile` both take an `as_of`, so nothing later leaks in.
- **`cmon tune`**: grid-searches the model parameters (recency half-life, Fourier harmonics for
  the daily cycle, and how much to trust the profile versus the flat rate) against your own
  history, fitting on the older part and validating on the newer one. Refuses to persist unless
  the winner beats the current parameters on that unseen slice and at least 200 predictions are
  scoreable. Stored as one small JSON row in `meta`; `--show` prints the active values, `--reset`
  restores defaults.
- **`quiet_hours` preference**: declares the local hours you don't work (`cmon config set
  quiet_hours 2-7`, ranges may wrap midnight). A deterministic backstop that applies from the
  first run, before any profile can be learned, and overrides the learned shape where they
  disagree. `CMON_QUIET_HOURS` overrides without persisting.

### Changed

- **Profile-weighted projection**: `watch`, `now`, `now --advice`, and rate-based alerts no
  longer extrapolate linearly (`rate × remaining hours`). cmon learns when you actually use
  Claude (24 local-hour buckets × weekday multiplier) and integrates that intensity over the
  time left until reset, anchored to the current EWMA rate — projections stop assuming you burn
  quota through the night at your daytime pace. Falls back to linear without enough history;
  output is marked *profile-weighted* when active.

### Fixed

- **The profile no longer invents overnight usage.** It was learned by spreading each
  `weekly_all` snapshot delta evenly across every hour its gap covered, accepting gaps up to
  48h — so a snapshot at midnight followed by one the next afternoon credited 02:00–07:00 with
  work done at 14:00. Those wide gaps then dominated the totals (31.6h of the 33.7h in the
  00:00 bucket) and flattened the shape until every hour looked equally busy. The profile is now
  learned from **request timestamps** in `token_log`, where an hour with no requests is an
  observed zero rather than an unknown. On real data the learned shape goes from flat
  (0.75–1.47, with 05:00 as the apparent "peak") to 0.02 at 04:00 and 2.31 at 21:00, and a 23h
  projection with 8h to reset drops from 90% to 69%. Snapshot pairs remain the fallback, now
  restricted to pairs under 1.5h apart, with unobserved hours interpolated from their neighbours
  instead of borrowing the daily mean — which is exactly what kept 3am looking busy.
- **EWMA anchor mismatch**, caught by the new replay backtest. The anchor divided the measured
  rate by `intensity(now)`, treating an EWMA average as this instant's intensity; for the weekly
  window that average spans ~12h and already mixes evening peaks with the night, so anchoring it
  to a peak-hour intensity made weekly projections quietly pessimistic. Dividing by the intensity
  averaged under the same exponential kernel takes horizons past 6h from MAE 15.55 / bias −9.13
  (worse than linear's 14.15) to MAE 11.31 — now clearly better than linear.
- Recency now decays with a half-life instead of a hard cutoff, so the learned shape drifts with
  your routine rather than jumping when a day falls out of a fixed window.
- The daily cycle can now be smoothed by a **weighted Fourier fit** (2–3 harmonics) instead of the
  3-point circular smoother — cyclic by construction, so hour 23 flows into hour 0, and only 5–7
  parameters for 24 buckets, which regresses noise instead of interpolating it. Off by default
  until `cmon tune` finds evidence for it. (Polynomial interpolation was considered and rejected:
  it doesn't close the cycle, oscillates on noisy buckets, and can go negative.)
- Replay and tuning memoize past rates and locate matching snapshots by bisection, roughly halving
  the cost per tuning candidate.

## [0.1.4] — 2026-07-08

### Added

- **`cmon config`**: persisted user preferences (`~/.cmon/config.json`), starting with
  `timezone` — `set`/`get`/`list`/`clear` subcommands, plus `CMON_TZ` for a quick
  non-persisted override. `watch`, `trends`, and `burn` now display timestamps in the
  resolved timezone instead of hardcoded UTC (stored data and internal calculations
  stay in UTC).
- **Per-window time-based alerts**: `collect --alert` now fires a desktop notification when the
  **weekly** window nears its reset too, not just the 5h window. New `CMON_ALERT_LEAD_WEEKLY`
  (default 180min) sets the weekly lead; `CMON_ALERT_LEAD` still governs the 5h session (default
  60min). Each window deduplicates on its own reset, so both fire once per cycle, independently.

### Changed

- **`cmon watch` default poll interval** raised 30s → 45s (fewer requests, lighter background use).
- **`cmon plot` now self-provisions its libraries.** If matplotlib/seaborn/pandas aren't
  installed, the first `plot` run installs them automatically (pip, then `uv pip` as a
  fallback for uv-managed envs), instead of exiting with instructions. `uv sync --extra plot`
  still works to pre-install them.

### Removed

- **`pandas` (and its `numpy` stack) dropped from the core dependencies.** It was only a
  convenience layer over DuckDB, not doing real analytics, and pulled a ~55 MB compiled
  stack into every install. All core code paths now use native DuckDB fetch + plain Python
  (SQL aggregation for summaries/burn, `unnest`-based bulk insert for log scanning), so a
  default `uv sync`/`pip install` is much lighter. `pytz` (pure-Python, tiny) is now an
  explicit dependency because DuckDB needs it to convert `TIMESTAMPTZ` ↔ Python `datetime`
  (it used to arrive transitively via pandas). Plotting still uses pandas, now declared in
  the optional `plot` extra alongside matplotlib/seaborn.

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
