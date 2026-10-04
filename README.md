# Claude Monitor

[![CI](https://github.com/LkHideki/cmon/actions/workflows/ci.yml/badge.svg)](https://github.com/LkHideki/cmon/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

CLI to track your Claude plan consumption over time. Reads the same endpoint
the app uses (`https://claude.ai/api/oauth/usage`), stores snapshots in DuckDB,
and displays consumption rate, projections, and charts.

## Installation

```bash
git clone https://github.com/LkHideki/cmon && cd cmon
uv sync                  # lightweight install (without plotting libraries)
uv sync --extra plot     # optional: pre-install the `plot` command's libs (matplotlib/seaborn/pandas)
```

`cmon plot` self-provisions: if the plotting libraries aren't installed, the first run
installs them automatically. `uv sync --extra plot` is only to pre-install them (e.g. offline).

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). Without uv,
`pip install -e .` also works.

## Token

`cmon` resolves the token in this order, stopping at the first match:

1. **`CLAUDE_OAUTH_TOKEN`** — environment variable (ideal for CI / override).
2. **OS secure vault** — Keychain (macOS), Credential Manager (Windows), or
   Secret Service (Linux). Stored once, never in plain text:

   ```bash
   cmon token set        # paste the token (hidden input); or:  echo $TOK | cmon token set
   cmon token status     # where the token comes from, masked
   cmon token clear      # remove from vault
   ```

   On a headless/CI box with no secure backend, `token set` refuses to write in
   cleartext (keyring's plaintext fallback) — override with
   `CMON_ALLOW_PLAINTEXT_KEYRING=1`, or just use `CLAUDE_OAUTH_TOKEN`.
3. **Claude Code credential** — if you're logged in, read directly from
   Keychain (macOS) or `~/.claude/.credentials.json` (Linux/Windows). Zero
   friction: nothing to configure.

**Claude Code's login is read-only.** cmon reads its access token and never redeems or
stores its `refresh_token`: Anthropic rotates refresh tokens on use, so a second client
spending it would log Claude Code out (cmon 0.5 did exactly that). Claude Code renews its
own token whenever it runs; if it has expired because Claude Code hasn't run for a while,
cmon says so and makes no request. After a `401` it re-reads Claude Code's credential once,
in case it was renewed meanwhile. The refresh chain older versions kept in the vault
(`claude-oauth-auto`) is deleted on first run.

In short, with Claude Code logged in you need nothing. Without it, `cmon token set` stores the token securely
on any system. Settings and step 1 can also live in `~/.cmon/.env` (see `.env.example`);
cmon never reads a `.env` from the directory you run it in, so a project's `.env` can't
set its alert hook or swap in another account's token. Run `cmon --help`
or `cmon token --help` for the rest.

## Config

`cmon` resolves display **timezone** in this order, stopping at the first match:

1. **`CMON_TZ`** — environment variable (quick override, doesn't persist).
2. **`~/.cmon/config.json`** — set via `cmon config set timezone <IANA name>`.
3. **UTC** — default if neither is set.

```bash
cmon config set timezone America/Sao_Paulo   # persist a preference
cmon config get timezone                     # show it
cmon config list                              # show all persisted preferences
cmon config clear timezone                    # remove it (or: cmon config clear --all)
```

Applies to displayed timestamps (`watch`, `trends`, `burn`); stored data and internal
calculations stay in UTC.

**`quiet_hours`** declares the local hours you don't work, so projections never spend them:

```bash
cmon config set quiet_hours 2-7      # ranges may wrap midnight ('23-2'), or list hours ('0-6,13')
```

It's a deterministic backstop — it applies from the first run, before there's enough history to
learn a profile, and overrides the learned shape where the two disagree. Usually you don't need it:
with Claude Code transcripts available, cmon learns your real quiet hours on its own (see
[`cmon now`](#cmon-now--current-usage--pacing)). Both preferences are validated on `set`;
`CMON_QUIET_HOURS` overrides without persisting.

## Usage

```bash
uv run cmon now       # current usage + reset + rate/projection (--advice adds pacing tips)
uv run cmon status    # one-liner for statusline/tmux/prompt
uv run cmon watch     # live TUI, self-updating (Ctrl-C exits)
uv run cmon wait      # block until 5h window resets, then notify
uv run cmon collect   # save 1 snapshot to database (with UTC timestamp)
uv run cmon trends    # consumption history: per-label summary + per-cycle peaks/anomaly
uv run cmon burn      # tokens & estimated US$ (from local Claude Code logs)
uv run cmon plot      # charts -> usage.png
uv run cmon backtest  # how accurate past projections were (--replay scores existing history)
uv run cmon tune      # fit the projection's parameters to your own history
uv run cmon install   # schedule background collection in OS scheduler
```

History lives in `~/.cmon/usage.duckdb` whatever directory you run from. Global option
`--db PATH` (before the subcommand) overrides `CMON_DB`: `uv run cmon --db /tmp/x.duckdb now`.

Up to 0.5.0 the default was `./usage.duckdb`, so history may be split across the
directories cmon ran in. Merge them once (sources are read-only, overlaps deduplicated):

```sh
cmon import ~/usage.duckdb ~/Projects/*/usage.duckdb
```

### `cmon status` — statusline

One compact line, ideal for status bar / tmux / prompt. Exits with code 0
and prints `cmon offline` if the network fails (doesn't break the statusline):

```
5h 18% · wk 42% · reset 3h18m
```

It reads the local cache. Only when the cached 5h window has already reset does it call
the API (5 s timeout; after a failure it serves the cache for 2 min before retrying), so
it never shows a dead cycle and never blocks a prompt. `--live` always queries the API.

`--json` prints a versioned payload (`version`, `offline`, `observed_at`, `age_s`,
`providers.claude.windows[]`) for scripts and the menu bar app.

### Menu bar (macOS)

`menubar/` holds **cmon Bar**, a native menu bar item (Swift, no third-party app). The
title shows the quota as `2•44••91` — 5h %, weekly %, minutes to the 5h reset (blank
until 5h usage passes 85% or the reset is under 100 min away, e.g. `5•44•75•91`), hours
to the weekly reset — and a click
opens a per-window breakdown with progress bars, reset times and when the next update is
due.

```sh
menubar/install.sh            # build with swiftc, start now and at login
menubar/install.sh uninstall
menubar/test.sh               # logic tests (title, refresh rules, settings)
```

Everything is configured in `~/.cmon/bar.json`, re-read on every refresh (an invalid file
is reported in the menu and defaults are used):

- **Title** — `segments` joined by `separator`; an empty segment keeps its slot, so
  positions never shift. Placeholders: `{5h}` `{wk}` `{reset}` (minutes) `{reset_hm}` —
  both blank unless 5h usage is above `reset_above_percent` (85) or the reset is at most
  `reset_within_min` (100) minutes away; while shown they count down on their own —
  `{wk_reset_h}` (hours)
  `{wk_reset_hm}` `{age}` (minutes since the data was read). The title dims when the data
  is older than `stale_minutes`.
- **Refresh schedule** — `refresh` is a list of rules for how often the bar queries the
  API (`status --live`, which also refreshes the cache the statusline reads). A rule
  applies when all its conditions hold: `reset_within_min` (the 5h reset is at most that
  many minutes away), `usage_above_pct` (5h usage is above it), or none for the default.
  The shortest `every_s` among applicable rules wins (minimum 30 s). Defaults:

  ```json
  "refresh": [
    { "every_s": 600 },
    { "reset_within_min": 120, "every_s": 180 },
    { "reset_within_min": 90,  "every_s": 120 },
    { "reset_within_min": 60,  "every_s": 59 },
    { "usage_above_pct": 87,   "every_s": 59 }
  ]
  ```

  The bar wakes exactly when a tighter rule starts applying. A failed call keeps the last
  numbers, dims the title and retries after a full interval. Each call costs ~0.3 s of
  CPU; at the defaults that is 6 calls an hour, 60 in the last hour before a reset.
- **`watch_command`** — what "Open cmon watch" runs; defaults to Terminal.app. For another
  terminal, e.g. `"open -na Ghostty --args -e $HOME/.local/bin/cmon watch"`.

Countdowns switch to seconds in the last minute and, while the menu is open or the 5h
reset is under a minute away, redraw every second (local text only, no extra `cmon`
runs); when the window rolls over the bar fetches the new cycle right away. Quit from the
menu and it returns at next login, or now with `open ~/Applications/"cmon Bar.app"`.

### `cmon wait` — notify when ready

Blocks until the window resets and then triggers a native notification — so you
can resume the second the 5h limit clears. Or use `--at N` to notify when *reaching* N%:

```bash
uv run cmon wait                      # wait for 5h to reset
uv run cmon wait --window weekly_all  # wait for weekly to reset
uv run cmon wait --at 80              # notify when 5h reaches 80%
```

### `cmon trends` — consumption history

Prints a per-label **summary** first — snapshots, peak %, and total consumed % over the
window (`--since 24h`/`7d`/ISO date; `--json` for scripts/pipes) — then the reset-aware
**cycle breakdown**: it segments history at each reset and shows the peak of each cycle, the
delta versus the previous one, and a warning if the current cycle deviates from the average.

```bash
uv run cmon trends                # summary + cycle breakdown
uv run cmon trends --since 24h    # summary over the last 24h (accepts 7d or ISO date)
uv run cmon trends --json         # JSON summary for script/pipe
```

### `cmon burn` — tokens & cost (from logs)

While the rest of `cmon` reads the **official %** from the endpoint, `burn` mines
the local Claude Code transcripts (`~/.claude/projects/**/*.jsonl`) to provide what
the endpoint doesn't expose: **tokens and estimated US$ by model, day, project, or session**
— retroactive, offline, no token required.

```bash
uv run cmon burn                    # by model, last 30 days (default)
uv run cmon burn --by surface       # by client: terminal / vscode / app / sdk (-p)
uv run cmon burn --by project       # attribution by project (where your plan went)
uv run cmon burn --since 7d         # smaller window (24h, 7d, ISO date…)
uv run cmon burn --since all        # full history (but Claude Code only keeps ~30d)
uv run cmon burn --json
uv run cmon burn --all-accounts     # include Desktop sessions of other accounts (see below)
```

**Which account counts.** Only the account the terminal's Claude Code is signed into
(`~/.claude.json`) — the one whose quota cmon reads. The Claude Desktop app may be signed
into another account yet writes its Claude Code sessions to the same transcripts folder;
those count only when Desktop's session index
(`~/Library/Application Support/Claude/claude-code-sessions/<account>/…`) ties them to the
terminal account. Sessions it can't attribute are left out of burn, the `watch` burn line,
the hourly profile and `plot`, and `burn` reports how much was excluded. VS Code and
terminal sessions share the terminal's login, so they always count.

The default window is **30 days** — Claude Code deletes transcripts older than
that, so beyond 30d there's usually no data. Use `--since all` for everything
available, or `--since 7d`/`24h`/ISO date for smaller windows.

`--by surface` separates by where you used it (the `entrypoint` field in logs). Only
Claude Code, though — different accounts (by email) **are not** separable: transcripts
don't record the account, and claude.ai chat doesn't write logs.

Scanning is incremental (caches by `mtime`+size, deduplicates by `uuid`): the
first run reads everything (~tens of seconds on large bases), subsequent runs take
fractions of a second. The same numbers appear in `watch` (line *burn 5h*) and in
`now --advice` (model mix). Both are scoped to the **current 5h rate-limit window** (since the
last reset), not a rolling 5h, so they reflect only what you spent in this cycle.

`burn` also shows a **component breakdown** (input / output / cache read
/ cache write). Don't be alarmed by the total: in agentic use, **cache read + write
typically account for ~80% of the cost** — the model re-reading cached context with
each message, not new work. And the value is the **API-equivalent cost**
(pay-per-token): **you pay the subscription, not this** — the number shows the *value*
you extract from your plan (easily tens of times the monthly subscription).

Crossing both sources: the **API** tells you *where the wall is* (official % + reset), the
**logs** tell you *how you spent it* (which model/project drained it). Caveats: cost is
an **estimate** (price table editable at the top of [`cmon.py`](cmon.py); cache write
at 2×, TTL 1h), and logs cover **only Claude Code CLI** — usage on claude.ai web/desktop
doesn't appear (but counts toward the official %).

**The `<synthetic>` label** (usually `0%` in the model mix) is not a real model: Claude
Code stamps `model: "<synthetic>"` on messages it generates *locally* instead of calling
the API — interrupts (`[Request interrupted by user]`), injected notices, aborted turns.
Their `usage` block is all zeros, so they cost nothing and don't move your totals; they
appear only because `cmon` mines every transcript line carrying a `usage` field. Safe to
ignore — real consumption is under `Opus`/`Sonnet`/`Haiku`/`Fable`.

### `cmon watch` — live TUI

Self-updating dashboard: percent colored by window (green/yellow/red), rate `%/h`,
projection at reset, and alerts when you'll hit 100% before reset. Great to leave open
in a corner of the terminal. Each read is recorded to the database (deduped), so leaving
`watch` open also builds history. `-u/--usage` adds a bar-chart column.

```bash
uv run cmon watch                 # ~30s, self-tuning
uv run cmon watch -n 10           # start at ~10s
uv run cmon watch -u              # with the Usage bar column
```

If claude.ai's Cloudflare starts returning **403** (bot-detection on the fixed polling
cadence), `watch` self-tunes: it backs the interval off on each error (up to 5min), adds
jitter, eases back once reads succeed again, and remembers the safe interval for the next
run — so you can leave it open without tripping the block.

### Alerts

`collect --alert` fires two kinds of alert to stderr (cron emails them) plus a best-effort
native notification (macOS/Linux):

- **rate-based** — at the current pace a window will hit **100% before reset** (also shown by
  `cmon now`);
- **time-based** — a window is within its lead of resetting: the **5h window in ≤ `CMON_ALERT_LEAD`
  minutes** (default 60) and the **weekly window in ≤ `CMON_ALERT_LEAD_WEEKLY` minutes** (default
  180). Each fires once per cycle (deduped on its own reset), as a desktop notification.

Set **`CMON_HOOK`** to run your own command on the time-based alert: cmon executes it via the
shell with the message in `$CMON_ALERT_MSG` (best-effort — a broken hook never breaks
collection). Pipe it into a bell, a webhook, `tmux display-message`, etc.

```cron
*/20 * * * * cd ~/cmon && /path/to/uv run cmon collect --alert
```

```bash
export CMON_ALERT_LEAD=30                                        # alert 30min before the 5h reset
export CMON_ALERT_LEAD_WEEKLY=180                               # alert 3h before the weekly reset
export CMON_HOOK='tmux display-message "cmon: $CMON_ALERT_MSG"'  # run on that alert
```

### `cmon now` — current usage & pacing

`cmon now` answers immediately "how long until my 5h window resets" and,
if there's history, projects whether you'll hit the limit before then:

```
Current usage:
  Current session  █··················   7%    resets in   4h 25min
  All models       ████████··········  41%    resets in 3d 22h
  Fable only       ████████··········  43%    resets in 3d 22h  ←active

5h window: 7% used — expires in 4h 25min.
Rate: 2.1%/h → projection at reset: 16%.
```

Add **`--advice`** for the full pacing view — the goal being to spend close to **100% of the
weekly limit** by reset, without exhausting early and without hitting the **5h window** (which
locks you out). For each window it shows the observed rate, the target `%/h` to zero slack, and
the projection at reset:

- **projection < 100%** → *upside*: quota left, you can intensify or use a stronger model;
- **projection > 100%** → *shortfall*: how many hours until you hit 100% before reset and how
  much you need to throttle.

The rate cuts at the last reset (so it adapts to 5h, 7d, or 72h windows — Anthropic resets the
"weekly" at a fixed time, not always exactly 7 days) and is **exponentially weighted**: recent
snapshots count more (half-life ~1h for the 5h window, ~12h weekly), so the projection tracks your
current pace rather than a flat cycle average.

The projection itself is **profile-weighted**, not linear: cmon learns when you actually use Claude
(24 local-hour buckets × a weekday multiplier) and integrates that intensity over the hours left
until reset, anchored to the current rate — so a projection taken at 22h doesn't assume you'll keep
burning through the night, and one taken mid-afternoon accounts for the busy evening ahead. On real
data, a 23h projection with 8h to reset drops from 90% (linear) to 69%.

The shape is learned from **request timestamps** in your Claude Code transcripts (the same
`token_log` that powers `burn`), because there an hour with no requests is an *observed zero*.
Snapshot percents can't say that: they move in whole points, and the gap between two of them says
nothing about *when* inside it the work happened — spreading a delta evenly across a 20h gap is
what used to credit 3am with your afternoon's usage. Recency decays with a half-life, so the shape
follows your current routine instead of jumping when a day leaves a fixed window. Hours you rarely
use are additionally **damped by occupancy** — how often the hour sees real activity, not how hard
its rare bursts were — so one heavy 4am doesn't leave every future 4am projected at burst-level
intensity (the mean alone would keep a third of that burst there forever). Without
transcripts it falls back to dense snapshot pairs, then to `quiet_hours`, then to linear;
`watch`, `now`, `--advice`, and the rate-based alerts all share it (marked *profile-weighted* when
active). See [`cmon backtest`](#cmon-backtest--is-the-projection-any-good) for how well it does.
Finally, `--advice` sends the
numbers to **Claude Sonnet** (`claude -p`, cheap) for 3 actionable tips; use `--no-ai` for local
projections only, no quota cost.

### `cmon backtest` — is the projection any good?

Every projection is logged with its horizon and settled against reality once that time passes, so
the model can be checked instead of trusted. `cmon backtest` scores the result — MAE in percentage
points, plus the bias (positive = projecting too high) — for the active model and for the plain
linear baseline, which is the only way to tell whether the profile earns its complexity:

```bash
cmon backtest              # score the forecasts recorded so far
cmon backtest --replay     # score your existing history right now, no waiting
```

`--replay` rebuilds each past prediction from the data that existed *at that moment* — the rate and
the profile are both recomputed with everything later hidden — so it can't flatter itself with
hindsight. On a 6-week history:

```
  model        horizon       n     MAE    bias
  learned      <=1h       3540    4.38   +1.95
  learned      1-6h       3785    9.73   +2.72
  learned      >6h         807   11.31   -6.87
  linear       <=1h       3540    4.31   +1.92
  linear       1-6h       3785    9.69   +2.26
  linear       >6h         807   14.15   -2.75
```

The profile pays off where it should — long horizons, the ones that cross a night — and is a tie
one hour out, where there's no shape to exploit. This measurement is also what caught a real bug in
the anchor (see the CHANGELOG).

### `cmon tune` — fit the model to your own history

```bash
cmon tune --show     # active parameters
cmon tune            # grid-search and persist the winner
cmon tune --reset    # back to defaults
```

Four parameters (recency half-life, number of Fourier harmonics used to smooth the daily cycle,
how much of the profile to trust versus the flat rate, and how hard to damp rarely-used hours by
occupancy) are fitted on the older part of your history and scored on the newer part — never on
the data used to choose them. It **refuses to save anything** unless the winner beats the current
parameters on that unseen slice, and unless there are at least 200 scoreable predictions; a grid
search over a few dozen points fits noise, not your routine. The result is four numbers stored as
a single small JSON row in the database, next to the history they were fitted on.

## Continuous collection

`plot`/`trends`/alerts become useful with history. Every command that queries the
API — `now`, `watch`, `wait` — already records its reading (deduped within
`CMON_DEDUP_SECS`), so history accrues from normal use, not only from scheduled `collect`.
Persisting is best-effort: if the single-writer DB is busy (e.g. `watch` is open) the read
still succeeds, it just doesn't save that one point. For unattended, regular sampling, leave
`collect` scheduled in the OS native scheduler:

```bash
uv run cmon install            # every 20min (launchd/systemd/schtasks)
uv run cmon install -i 10      # every 10min
uv run cmon install --dry-run  # only show what it would do
uv run cmon uninstall          # remove
```

In the background the token comes from the OS vault or Claude Code credential — your shell's
`CLAUDE_OAUTH_TOKEN` env var **is not** inherited, so run `cmon token set`
if that's your case. Prefer manual cron? Still works:

```cron
*/20 * * * * cd ~/cmon && /path/to/uv run cmon collect --alert
```

## How it works

- **Source**: `limits[]` array from the endpoint — `session` (5h window),
  `weekly_all` (all models), and `weekly_scoped` (per-model, e.g. Fable).
- **Consumption**: difference in `percent` between snapshots; drops = window reset
  (discarded), not consumption.
- Requires the `User-Agent: claude-cli/...` header, otherwise claude.ai's Cloudflare
  responds with 403.
- **Resilience**: `fetch` retries on 429/5xx/network with backoff (respects a clamped
  `Retry-After`); 401/403 fail with a clear message, but `watch` turns a 403 into an adaptive
  backoff (see above) rather than giving up, and token resolution self-heals a dead refresh
  chain from the Claude Code credential. `collect` deduplicates very close readings
  (`CMON_DEDUP_SECS`, default 60s; `--force` ignores) and exits with code ≠ 0 on failure, so
  cron logs the error instead of silencing it.

## Development

Everything lives in a single file, [`cmon.py`](cmon.py) — commands are functions `now`,
`collect`, `watch`, etc., wired to argparse in `main()`. Easy to read top to bottom.

```bash
uv sync --extra plot         # install everything, including plotting libs
uv run ruff check .          # lint (config in pyproject.toml)
uv run ruff check --fix .    # auto-fix what it can
uv run cmon <cmd>            # run straight from source
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff + CLI smoke tests
on Python 3.11–3.13. PRs welcome: keep `ruff` green and the file style lean. Useful
environment variables: `CMON_DB` (database path), `CMON_RETRIES`, `CMON_DEDUP_SECS`,
`CMON_ALERT_LEAD` / `CMON_ALERT_LEAD_WEEKLY` (minutes before the 5h / weekly reset to alert),
`CMON_HOOK` (command run on that alert),
`CMON_OAUTH_CLIENT_ID` / `CMON_OAUTH_TOKEN_URL` (OAuth refresh overrides; URL allowlisted to
`*.anthropic.com`), `CMON_ALLOW_PLAINTEXT_KEYRING` (permit saving to a cleartext keyring backend),
`CMON_TZ` (display timezone override; see [Config](#config)).

## Warning

Uses a private, undocumented Anthropic endpoint; may change without notice.
Only accesses your own account. License [MIT](LICENSE).
