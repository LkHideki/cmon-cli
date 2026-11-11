# Claude Monitor

[![CI](https://github.com/LkHideki/cmon-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/LkHideki/cmon-cli/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

CLI to track your Claude plan consumption over time. Reads the same endpoint
the app uses (`https://claude.ai/api/oauth/usage`), stores snapshots in DuckDB,
and displays consumption rate, projections, and charts. On macOS it adds **cmon Bar**, a
native menu bar item showing your limits at a glance.

cmon tracks Claude only for now. Its database is already prepared for other providers (see
[the Codex plan](docs/plans/codex-provider.md)), but no command reads Codex data yet.

| Path | What it is |
|---|---|
| [`cmon.py`](cmon.py) | The whole CLI, as a single module |
| [`menubar/`](menubar/) | cmon Bar, the macOS app, in Swift |
| [`scripts/setup.sh`](scripts/setup.sh) | What `make install`, `make uninstall` and `make doctor` run |
| [`tests/`](tests/) | Python tests; they never touch your real data or the network |
| [`docs/plans/`](docs/plans/) | Design plans, in Portuguese; not all of them are implemented |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each release, and how to upgrade |

## Quick start (macOS)

Needs [uv](https://docs.astral.sh/uv/) and the Xcode Command Line Tools; `make install` tells
you the exact command if either is missing. Log in to Claude Code first: cmon reads its login.

```bash
git clone https://github.com/LkHideki/cmon-cli && cd cmon-cli
make install     # cmon command, ~/Applications/cmon Bar.app, background collection, then a check
make doctor      # what's wrong and how to fix it
make uninstall   # removes all of it, keeps your history in ~/.cmon
```

macOS may ask to allow Keychain access when cmon first reads Claude Code's login; choose
**Always Allow**, or the menu bar can't refresh. The menu bar then reads `90% 75'  44% 91`:
the 5h window on the left and the weekly limit on the right, each as percent used and time
until it resets (plain numbers are hours, `'` minutes). Click it for details.

### Ask Claude to install it

Paste this into Claude Code:

> Install cmon from https://github.com/LkHideki/cmon-cli following its AGENTS.md, and tell me
> if anything needs me.

[AGENTS.md](AGENTS.md) gives assistants the steps, what each failure means, what the setup
changes, and what they must leave to you, such as logging in.

## Installation (CLI only, any OS)

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). cmon isn't on PyPI, and the PyPI
package named `cmon` is an unrelated project, so install from this repository:

```bash
uv tool install git+https://github.com/LkHideki/cmon-cli
cmon now
```

To work on the code instead, clone it and run `uv sync`; commands then run as `uv run cmon …`.
`uv sync --extra plot` pre-installs the libraries for `cmon plot`, which otherwise installs
them on first use.

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

**`keepalive`** starts the next 5h window as soon as the last one ends. Left alone, a window that
rolls over stays parked at `0% –` until your next request; with this on, cmon sends that request
itself:

```bash
cmon config set keepalive on    # off by default
cmon keepalive                  # send one now
```

The request is one small Haiku question through the `claude` command (a cosine of a random number,
different every time), so it needs Claude Code logged in to the account cmon reads, and it counts
towards your limits like any other, by under 1,000 tokens. cmon notices the rollover at the
next read: within seconds with cmon Bar running, otherwise at the next `cmon collect`. An attempt
that doesn't open the window is retried after 30 minutes; attempts are logged to
`~/.cmon/keepalive.log`. A sleeping computer sends nothing, so the next window starts when it wakes.

Windows then run back to back whether you work or not, so you may sit down with only part of one
left. The reset comes sooner than if your first request had started it.

**`sync_dir`** and **`device_name`** share usage between your machines; see
[`cmon sync`](#cmon-sync--usage-by-device).

## Usage

The examples use `uv run cmon`, for a clone of this repository. After `make install` or
`uv tool install`, just use `cmon`.

```bash
uv run cmon now       # current usage + reset + rate/projection (--advice adds pacing tips)
uv run cmon status    # one-liner for statusline/tmux/prompt
uv run cmon watch     # live TUI, self-updating (Ctrl-C exits)
uv run cmon wait      # block until 5h window resets, then notify
uv run cmon collect   # save 1 snapshot to database (with UTC timestamp)
uv run cmon trends    # consumption history: per-label summary + per-cycle peaks/anomaly
uv run cmon burn      # tokens & estimated US$ (from local Claude Code logs)
uv run cmon sync      # share usage with your other devices through a synced folder
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
title shows the quota as `90% 75'  44% 91` — 5h window, then weekly limit, each as percent
used and time until it resets: plain hours from 2 h up, minutes with `'` below that and
seconds with `"` in the last minute — and a click
opens a per-window breakdown with progress bars, reset times and when the next update is
due. Under **By client**, one bar per window shows who used this cycle's percent — VS Code,
Terminal, Desktop app, SDK — and how much of it nothing on this Mac logged (*claude.ai &
elsewhere*). The split comes from the background `cmon collect` (see
[`--plan`](#cmon-burn--tokens--cost-from-logs)), so it appears once collection is installed.

```sh
make install                  # everything, including the cmon command this app needs
menubar/install.sh            # only the app: build with swiftc, start now and at login
menubar/install.sh uninstall
menubar/test.sh               # logic tests (title, refresh rules, settings)
```

Everything is configured in `~/.cmon/bar.json`, re-read on every refresh (an invalid file
is reported in the menu and defaults are used):

- **Title** — `segments` joined by `separator`; an empty segment keeps its slot, so
  positions never shift. Placeholders: `{5h_pair}` `{wk_pair}` (`90% 75'`, always shown);
  with their unit: `{5h_pct}` `{wk_pct}` `{reset_short}`
  `{wk_reset_short}`; bare numbers: `{5h}` `{wk}` `{reset}` (minutes) `{reset_hm}` — the 5h
  reset ones are blank unless 5h usage is above `reset_above_percent` (85) or the reset is at most
  `reset_within_min` (100) minutes away; while shown they count down on their own —
  `{wk_reset_h}` (hours)
  `{wk_reset_hm}` `{age}` (minutes since the data was read); `{5h_bar}` `{wk_bar}` draw usage
  as a block and `{5h_dot}` `{wk_dot}` as a traffic light; `{top_client}` `{5h_top_client}` name
  the client that used the most of the week or the 5h window (`VS Code 57%`). The title dims when the data is
  older than `stale_minutes`. With `"hide_empty": true`, a word holding a blank placeholder and
  any segment left empty disappear, which suits labelled layouts:

  ```json
  "segments": ["5h {5h_pct} {reset_short}", "7d {wk_pct} {wk_reset_short}"],
  "separator": " · ",
  "hide_empty": true
  ```

  This reads `5h 2% · 7d 44% 91h`, and `5h 90% 75m · 7d 44% 91h` near the reset.
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
uv run cmon burn --by surface --plan   # who spent this week's percent (see below)
uv run cmon burn --by project --surface vscode   # only some clients (comma-separated)
uv run cmon burn --by device        # by machine, once 'cmon sync' is set up
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
don't record the account, and claude.ai chat doesn't write logs. A client cmon doesn't know yet
shows up under its raw entrypoint.

**Share of the plan: `--plan`.** US$ is what the API would charge; what runs out on a subscription
is the percent. `--plan` splits the official percent across the groups of any `--by`:

```text
$ cmon burn --by surface --plan
Consumption by surface since 2026-11-04 10:59 -03 (API equivalent):
  vscode                  1108.3M tok   US$    839.39     44.7 pp   59%
  terminal                 832.4M tok   US$    522.88     26.5 pp   35%
  sdk (-p/agent)            90.4M tok   US$     82.25      4.7 pp    6%
  other (no local log)                                     0.0 pp    0%
  TOTAL                   2031.1M tok   US$   1444.51     76.0 pp  100%
```

Each rise of the percent between two snapshots is charged to the groups in proportion to the
API-equivalent cost they logged meanwhile; cost rather than tokens, because the limit weighs models
differently and cache reads are cheap. The percent is an integer, so snapshots where it didn't move
pass their cost on to the next one that did. A rise with nothing logged is **other**: claude.ai
chat, other devices, cloud sessions. It needs snapshot history (`cmon install`, `watch`), it's an
estimate, and it gets better the closer together the snapshots are.

Without `--since` it covers the window's current cycle. `--plan` takes `weekly` (default), `5h` or a
scoped model such as `fable`. With `--surface` or `--device`, the clients filtered out keep their
points as *rest (filtered out)*, so what's left isn't inflated. `--json` with `--plan` prints an
object with `window`, `since`, `cycles`, `total_pp`, `other_pp`, `unpriced_models` (see *Prices*
below) and `groups`, each with `plan_pp`.

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
an **estimate** (prices below; cache write at 2×, TTL 1h), and logs cover **only Claude Code
CLI** — usage on claude.ai web/desktop doesn't appear (but counts toward the official %).

**Prices: `~/.cmon/prices.json`.** cmon ships prices for Fable, Opus, Sonnet and Haiku. The file,
optional, replaces any of them and adds new ones, in US$ per million tokens, in the order input,
output, cache read, cache write:

```json
{
  "mythos": [20, 100, 2, 40],
  "opus-4-1": [15, 75, 1.5, 30]
}
```

Each key is matched, case-insensitively, as a substring of the model id in the transcripts
(`claude-opus-4-1-20250805`), and the longest matching key wins whatever its place in the file, so
`opus-4-1` takes precedence over the built-in `opus`. Built-in keys you don't list keep their price.
An invalid file (not JSON, a value that isn't four numbers ≥ 0, the same key twice in different
case) is ignored as a whole, with one warning, and the built-in prices apply. Edits take effect on
the next run, and in a running `watch` on its next refresh.

A model that matches no key costs US$ 0. `burn` then warns on stderr, naming the model and its
tokens, because that zero also skews `--plan`: the percent the model used goes to whichever groups
logged a cost in the same interval, or to *other*. `--json` reports the same thing as
`unpriced_models`: a list at the top of the `--plan` object, and on each group that has such tokens
in both forms. cmon doesn't guess a price for a model it doesn't know.

**The `<synthetic>` label** (usually `0%` in the model mix) is not a real model: Claude
Code stamps `model: "<synthetic>"` on messages it generates *locally* instead of calling
the API — interrupts (`[Request interrupted by user]`), injected notices, aborted turns.
Their `usage` block is all zeros, so they cost nothing and don't move your totals; they
appear only because `cmon` mines every transcript line carrying a `usage` field. Safe to
ignore — real consumption is under `Opus`/`Sonnet`/`Haiku`/`Fable`.

### `cmon sync` — usage by device

Each machine only sees its own transcripts. To put them together, point every machine at a folder
they already sync (iCloud Drive, Dropbox, Syncthing, a network share):

```bash
cmon config set sync_dir ~/Library/Mobile\ Documents/com~apple~CloudDocs/cmon
cmon config set device_name work-mbp   # optional; defaults to the short hostname
cmon sync                              # once now; 'collect' does it after every collection
cmon burn --by device --plan
```

Each machine writes its own rows to `<sync_dir>/<device id>.parquet` and imports the other
machines' files. It exports only what it read from its own transcripts, so nothing echoes back,
and only the terminal account's sessions. Each file records its account, and a file from another
Claude account is skipped, since its usage doesn't count against this plan. Imports are keyed by
message, so running it twice, or a conflict copy made by the sync tool, adds nothing. Renaming a
device relabels its history on the next sync.

The files carry project names and session ids, never prompts or code. They stay in your folder,
readable only by your user. There's no server.

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

Set **`CMON_HOOK`** to run your own command on every alert, of both kinds: cmon executes it via
the shell when the alert fires, so once per window cycle and never on the deduplicated repeats
(best-effort, with a 30s timeout: a broken hook never breaks collection). The alert reaches the
command in environment variables, which are always set and empty when unknown:

| Variable | Value |
|---|---|
| `CMON_ALERT_MSG` | The alert text, as shown on stderr |
| `CMON_ALERT_KIND` | `rate` (pace hits 100% before reset) or `lead` (reset is near) |
| `CMON_ALERT_KEY` | The window: `session` (5h) or `weekly_all` |
| `CMON_ALERT_PERCENT` | Percent of the window used, e.g. `87` or `87.5` |
| `CMON_ALERT_RESETS_AT` | When the window resets, ISO 8601 in UTC, e.g. `2026-11-04T02:00:00+00:00` |

The hook doesn't see cmon's `CLAUDE_OAUTH_TOKEN`. Pipe it into a bell, a webhook,
`tmux display-message`, etc.

```cron
*/20 * * * * cd ~/cmon && /path/to/uv run cmon collect --alert
```

```bash
export CMON_ALERT_LEAD=30                                        # alert 30min before the 5h reset
export CMON_ALERT_LEAD_WEEKLY=180                               # alert 3h before the weekly reset
export CMON_HOOK='tmux display-message "cmon: $CMON_ALERT_MSG"'  # run on every alert
```

A push to your phone through [ntfy.sh](https://ntfy.sh) (pick a topic name nobody will guess;
anyone who knows it can read it):

```bash
export CMON_HOOK='curl -fsS -m 10 -H "Title: cmon $CMON_ALERT_KIND $CMON_ALERT_KEY" \
  -d "$CMON_ALERT_MSG" https://ntfy.sh/my-cmon-topic-7f3k'
```

Any JSON webhook. It leaves out `CMON_ALERT_MSG`, which may hold quotes; the other variables
never do, so they are safe to splice into JSON:

```bash
export CMON_HOOK='curl -fsS -m 10 -H "Content-Type: application/json" -d "{\"kind\":\"$CMON_ALERT_KIND\",\"window\":\"$CMON_ALERT_KEY\",\"percent\":${CMON_ALERT_PERCENT:-null},\"resets_at\":\"$CMON_ALERT_RESETS_AT\"}" https://example.com/hooks/cmon'
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
`CMON_HOOK` (command run on every alert; see [Alerts](#alerts)),
`CMON_OAUTH_CLIENT_ID` / `CMON_OAUTH_TOKEN_URL` (OAuth refresh overrides; URL allowlisted to
`*.anthropic.com`), `CMON_ALLOW_PLAINTEXT_KEYRING` (permit saving to a cleartext keyring backend),
`CMON_TZ` (display timezone override; see [Config](#config)).

## Warning

Uses a private, undocumented Anthropic endpoint; may change without notice.
Only accesses your own account. License [MIT](LICENSE).
