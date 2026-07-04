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
uv sync --extra plot     # optional: enables the `plot` command (matplotlib/seaborn)
```

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
3. **Claude Code credential** — if you're logged in, read directly from
   Keychain (macOS) or `~/.claude/.credentials.json` (Linux/Windows). Zero
   friction: nothing to configure.

**Auto-refresh:** when the access token expires, `cmon` renews it automatically via
`refresh_token` and stores the new token in its own vault (`claude-oauth-auto`),
**without** rewriting the Claude Code credential. Renews proactively (reads
`expiresAt`) and reactively (if the API returns 401 — including when an old
`CLAUDE_OAUTH_TOKEN` is shadowing everything). Side effect: the first renewal
rotates Claude Code's `refresh_token`, so **it may ask for login once** the next
time it renews — after that the two tokens become independent. `token status`
shows validity; `client_id`/endpoint are configurable via `CMON_OAUTH_CLIENT_ID` /
`CMON_OAUTH_TOKEN_URL`.

In short, with Claude Code logged in you need nothing — and it keeps working
even with an expired token. Without it, `cmon token set` stores the token securely
on any system. `.env` still works for step 1 (see `.env.example`). Run `cmon --help`
or `cmon token --help` for the rest.

## Usage

```bash
uv run cmon now       # current usage + time to reset + rate/projection
uv run cmon status    # one-liner for statusline/tmux/prompt
uv run cmon watch     # live TUI, self-updating (Ctrl-C exits)
uv run cmon wait      # block until 5h window resets, then notify
uv run cmon collect   # save 1 snapshot to database (with UTC timestamp)
uv run cmon report    # summary of accumulated consumption
uv run cmon trends    # consumption by cycle (peak, delta vs previous, anomaly)
uv run cmon burn      # tokens & estimated US$ (from local Claude Code logs)
uv run cmon plot      # charts -> usage.png
uv run cmon tips      # pacing tips (use ~100% of weekly without exhausting 5h)
uv run cmon install   # schedule background collection in OS scheduler
```

Global option `--db PATH` (before the subcommand) overrides `CMON_DB`:
`uv run cmon --db ~/.cmon/usage.duckdb now`.

### `cmon status` — statusline

One compact line, ideal for status bar / tmux / prompt. Exits with code 0
and prints `cmon offline` if the network fails (doesn't break the statusline):

```
5h 18% · week 42% · reset 3h18m
```

### `cmon wait` — notify when ready

Blocks until the window resets and then triggers a native notification — so you
can resume the second the 5h limit clears. Or use `--at N` to notify when *reaching* N%:

```bash
uv run cmon wait                      # wait for 5h to reset
uv run cmon wait --window weekly_all  # wait for weekly to reset
uv run cmon wait --at 80              # notify when 5h reaches 80%
```

### `cmon trends` — cycle trends

Segments the history into cycles (cuts at each reset) and shows the peak of each,
the delta versus the previous cycle, and a warning if the current cycle deviates from the average.

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
```

The default window is **30 days** — Claude Code deletes transcripts older than
that, so beyond 30d there's usually no data. Use `--since all` for everything
available, or `--since 7d`/`24h`/ISO date for smaller windows.

`--by surface` separates by where you used it (the `entrypoint` field in logs). Only
Claude Code, though — different accounts (by email) **are not** separable: transcripts
don't record the account, and claude.ai chat doesn't write logs.

Scanning is incremental (caches by `mtime`+size, deduplicates by `uuid`): the
first run reads everything (~tens of seconds on large bases), subsequent runs take
fractions of a second. The same numbers appear in `watch` (line *burn 5h*) and in
`tips` (model mix from the last 5h, which grounds the model-switch tip).

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

Self-updating dashboard: colored bars by window (green/yellow/red), rate `%/h`,
projection at reset, and alerts when you'll hit 100% before reset. Great to leave open
in a corner of the terminal.

```bash
uv run cmon watch                 # update every 30s
uv run cmon watch -n 10           # every 10s
uv run cmon watch --collect       # save each reading to database while watching
```

### Alerts

`_alerts` warns when, **at the current rate, the window hits 100% before reset**.
They appear in `now` and `watch`; in `collect --alert` they go to stderr (cron emails
them) and trigger a best-effort native notification (macOS/Linux):

```cron
*/20 * * * * cd ~/cmon && /path/to/uv run cmon collect --alert
```

### `cmon report`

```bash
uv run cmon report --since 24h    # last 24h only (accepts 7d or ISO date)
uv run cmon report --json         # JSON output for script/pipe
```

### `cmon tips`

Goal: spend close to **100% of the weekly limit** by reset — without exhausting
early and without hitting the **5h window**, which locks you out. For each window shows
the observed rate, the target `%/h` to zero slack, and the projection at reset:

- **projection < 100%** → *upside*: quota left, you can intensify or use a stronger model;
- **projection > 100%** → *shortfall*: how many hours until you hit 100% before
  reset and how much you need to throttle.

The rate automatically cuts at the last reset, so it adapts to 5h, 7d windows
(or 72h — Anthropic resets the "weekly" at a fixed time, not always exactly 7 days).
Finally, it sends the numbers to **Claude Sonnet** (`claude -p`, cheap) which returns
3 actionable tips. Use `--no-ai` for local projections only, no quota cost.

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

## Continuous collection

`report`/`plot`/`trends`/alerts become useful with history. The easy way is to leave
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
- **Resilience**: `fetch` retries on 429/5xx/network with backoff (respects
  `Retry-After`); 401/403 fail immediately with clear message. `collect` deduplicates
  very close readings (`CMON_DEDUP_SECS`, default 60s; `--force` ignores) and exits
  with code ≠ 0 on failure, so cron logs the error instead of silencing it.

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
environment variables: `CMON_DB` (database path), `CMON_RETRIES`, `CMON_DEDUP_SECS`.

## Warning

Uses a private, undocumented Anthropic endpoint; may change without notice.
Only accesses your own account. License [MIT](LICENSE).
