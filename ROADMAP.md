# Roadmap

Candidate features for cmon, first surveyed on 2026-11-11 against 0.8.0. Nothing here is a
commitment: each entry says what it solves, which part of the current code it builds on, how much
work it is and what can go wrong. Larger items get their own plan in [`docs/plans/`](docs/plans/)
before any code.

Effort: **S** fits in one session, **M** takes a day or two, **L** needs its own plan and several
increments. Functions named here are in [`cmon.py`](cmon.py) unless stated otherwise.

## Picking an item

Items with an issue carry the [`roadmap`](https://github.com/LkHideki/cmon-cli/labels/roadmap)
label, and the smaller ones [`good first issue`](https://github.com/LkHideki/cmon-cli/labels/good%20first%20issue).
Comment on the issue before you start, so two people don't build the same thing; for an item with
no issue, open a feature request naming its ID. The risks listed under each item are what review
will look at, and [CONTRIBUTING.md](CONTRIBUTING.md) says what every change needs.

## Shipped in 0.9.0

The first ten items, picked for value against effort. The [0.9.0 changelog](CHANGELOG.md)
describes each one as it shipped; where one ended up differing from its first description is in
this file's history (`git show 147b106^:ROADMAP.md`, in Portuguese).

| ID | Feature |
|---|---|
| G2 | Prices outside the code, in `~/.cmon/prices.json`, and a warning for models with no price |
| A2 | `CMON_HOOK` on every `collect --alert` alert, with `CMON_ALERT_KIND`, `_KEY`, `_PERCENT` and `_RESETS_AT` |
| A1 | Alerts at chosen percents: `cmon config set alert_at "session=80,95 weekly_all=50,75,90"` |
| A4 | `cmon guard`, an exit code for scripts and hooks |
| E1 | `cmon statusline` for Claude Code, with the session's cost |
| B1 | Projection and exhaustion time in `status --json` and cmon Bar |
| B2 | Today's share of the weekly limit, in `cmon now` |
| C1 | Cache efficiency report: `burn --cache` |
| C4 | Cycle scoreboard, time blocked and what was left: `trends --scoreboard` |
| G1 | `cmon doctor` in the CLI itself, with `--json` |

## A. Alerts and automation

### A3. Notify when a blocked window frees up

Knowing that the 5h window has reset still means leaving `cmon wait` running. Scheduled
collection already sees the transition: notify when a window that was above a floor (say 90%) comes
back at zero or with a new `resets_at`. Effort S. It only matters to someone who was blocked, hence
the floor; without one it becomes a notification every five hours.

It belongs in `collect --alert`, next to `_fire_rate_alerts` and `_fire_threshold_alerts`, deduplicated
per cycle the same way. cmon Bar doesn't help here: it runs `cmon status --json --live`, and
`status` fires no alerts. This item also gives `CMON_HOOK` the `reset` kind, which the README
should then list among the values of `CMON_ALERT_KIND`.

### A5. Detect unusual consumption

A subagent looping overnight can eat the week before any current alert notices, because the rate
alert looks at the whole window. `token_log` has the cost of each message and session, and
`_hour_profile` knows how much each hour usually takes. Compare the cost of the last N minutes with
what's expected for that hour, and notify with the session and project responsible. Effort M. The
risk is false positives on heavy work days: start conservative, with a high multiple over the
profile, and calibrate with `backtest`.

### A6. Scheduled start of the 5h window

`keepalive` only acts once a window has ended and sat idle (`_window_parked`). There's no way to
choose *when* the window starts. A `keepalive_at` key (`06:30`, weekdays) would open the window
before the working day, so the first reset lands mid-morning instead of mid-afternoon. Effort S to
M. It shares the caveats of the current keepalive: it spends quota on the user's behalf, needs the
computer awake, and must be off by default.

## B. Projection and planning

### B3. Error band on projections

`backtest` measures the mean error for each horizon, yet the projection is shown as a bare number
in `now` and `watch`, and since 0.9.0 in `status --json` (`projected_percent`) and cmon Bar. Show
`69% ± 10` in `now` and `watch`, with the error of the matching horizon taken from
`_backtest_rows`. Effort S. With too few resolved forecasts there's no band to show, and the output
stays as it is today. If the band reaches `status --json`, it goes in a new optional field;
`projected_percent` stays a number.

### B4. What one percentage point is worth

`_plan_share` already matches the rise in percent with the cost logged over the same interval.
The ratio between the two, in API-equivalent US$ per point, measures the real size of the limit.
It answers how many points an hour of Opus costs against an hour of Sonnet, and it shows when
Anthropic changes the limit without notice: the ratio jumps from one cycle to the next. Effort M.
The estimate only holds over intervals where nearly all usage was local; intervals with a lot of
*other* must be dropped, and the result should be shown as a range.

### B5. Away days

`quiet_hours` declares hours without work; there's nothing similar for days. In a week with a
holiday or time off, the weekly projection runs high. `cmon config set away 2026-12-24..2026-12-26`
would zero the intensity of those days in `_project`'s integration, and the daily budget of B2
would skip them. Effort S.

## C. Usage analysis

The [usage-by-client](docs/plans/usage-by-client.md) plan lists more `burn --by` dimensions in its
backlog (subagents, skill, MCP server, plugin, effort, branch, version, fast mode), alerts per
client and a weekly summary. They're not repeated here.

### C2. Session timeline

`burn --by session` gives each session's total. `cmon session <id>` would show a session from the
inside: cost per turn, model, how the context grew, and where it was compacted. It explains why a
particular session cost 30 points. Effort M. Compaction and model switches have to be inferred from
the token sequence; nothing cmon stores today marks them.

### C3. Compare periods

`burn --compare` would put the requested period next to the previous one of the same length, with
the change per group: which project or client explains a more expensive week. Effort S. `token_log`
keeps rows after Claude Code deletes its transcripts, so on machines that have collected for a
while the comparison reaches past 30 days; on a recent install the previous period is empty, and
the output has to say so.

### C5. Heatmap by hour and weekday

The projection learns a 24-hour profile and a multiplier per weekday (`_hour_buckets_tokens` and
the `dow` multipliers of `_hour_profile`), and shows neither. One more panel in `cmon plot` and a
text version in `trends --heatmap`, with the full hour-by-weekday grid from `token_log`, let users
check that what cmon learned matches their routine, which also helps debug odd projections.
Effort S.

### C6. Export

`cmon export --format csv|parquet|json` for `snapshots`, `token_log` and cycles, with `--since`.
DuckDB already writes parquet in `sync_devices`. It serves anyone who wants the data in a
spreadsheet or BI tool without opening the database, which AGENTS.md advises against while `watch`
or `collect` hold the lock. Effort S. An export carries project names and session ids, as sync
does; the command's help should say so.

### C7. What the plan is worth

The README explains that the cost shown is the API equivalent. With a `plan_price` key in the
config, `burn` could close the loop: the month's API equivalent against the subscription price,
and the multiple between them. Effort S. Together with `trends --scoreboard`, it helps choose
between plans.

## D. cmon Bar (macOS)

### D1. Sparkline in the menu

The menu shows bars for the current state. A small chart per window, with the cycle's trajectory
and the ideal-pace line, would show at a glance whether usage runs above or below target, which
today only `cmon plot` shows. Effort M. It needs a history series that `status --json` doesn't
carry; better a separate `trends --json --series`, read only when the menu opens, than a heavier
payload on every refresh.

### D2. Per-project and per-model sections

The *By client* section came in 0.8.0. The same mechanism (`_refresh_client_share` with another
column) works for project and model, which answer "what is using it right now" better. Effort S to
M. There are many projects: show the top three and group the rest.

### D3. Native notifications with actions

Notifications go out through `osascript` (`_notify`): they appear under Script Editor's name, and
clicking one does nothing. Sent by the app with `UNUserNotificationCenter`, they'd carry cmon's
icon, a button to open `watch` and an option to mute until the next reset. Effort M. Untested
whether an app built with `swiftc` and signed ad hoc can get notification permission; that's the
first thing to check.

### D4. Settings window

*Settings…* opens `bar.json` in a text editor (`openSettings` in
[`menubar/CmonBar.swift`](menubar/CmonBar.swift)). A window with the title segments, the refresh
rules and a live preview would serve people who'd rather not edit JSON. Effort L, and the file
would remain the source of truth. Low priority: current users edit JSON without trouble.

## E. Integrations

### E2. `status --format`

The `bar.json` placeholders (`{5h_pct}`, `{reset_short}`, `{wk_bar}`, `{5h_proj}`…) exist only in
Swift. `cmon status --format '{5h_pct} {reset_short}'` with the same names would give tmux,
starship, waybar and polybar the customisation that is macOS-only today. Effort S to M. It means
two implementations of one vocabulary, in Python and Swift, which tend to drift; a shared file of
test cases read by both `tests/` and `menubar/Tests.swift` keeps them together.

### E3. MCP server

`cmon mcp` would expose `status` and `burn --plan` as tools, so an agent could check the quota and
decide on its own which model to use or when to stop. Effort M, with a new dependency. The gain is
small where the agent has a shell, since `cmon status --json` and `cmon guard` already answer; it
makes sense for clients without one, such as the desktop app.

### E4. Prometheus metrics

`cmon export --prom` in node_exporter's textfile format: percent per window, seconds to reset, cost
per model. Effort S, on top of C6. For people who already run Grafana at home or at work.

## F. Accounts

### F1. Profiles for more than one account

cmon reads one account, the one Claude Code is logged into in the terminal. Someone with a personal
and a work account can't follow both. `cmon --profile work` would keep token and database apart per
profile, and cmon Bar would show one group per profile. Effort L. It touches token resolution, the
most sensitive part of the code, and a secondary profile has no Claude Code login to read from: it
would depend on `cmon token set` and a token that expires with no automatic renewal.

## G. Data, installation and robustness

### G3. Backup before migrating

AGENTS.md asks users to copy `usage.duckdb` by hand before upgrading, because a migration can't be
read by older versions. `_ensure_schema` could make the copy itself, to `usage.duckdb.v<N>.bak`,
before opening the transaction, and `cmon db backup|restore` would cover the manual case. Effort S.
The database is small since the v2 schema compaction, but the copy should check free space and keep
only the latest one per version.

### G4. Keep the fields the endpoint already returns

`limits()` reads only `limits[]` and drops the rest of the response, and
`snapshots.metadata_json` exists but stays empty for Claude. Storing unknown fields there, and
warning once when a new `kind` shows up, guards against changes to the private endpoint and may
reveal useful data that is thrown away today. Whether the response holds anything beyond
`limits[]`, such as extra-usage credits, is a guess; the first step is to inspect a real response.
Effort S for the capture.

### G5. Notifications on Windows

`_notify` covers macOS and Linux; on Windows, alerts only reach the stderr of a scheduled task
nobody reads. A notification through PowerShell closes the gap. Effort S, but CI has no Windows
job, so it needs someone who can test it on a Windows machine.

### G6. Shell completion

`cmon completion zsh|bash|fish`, generated from the argparse parser built in `main()`, so new
commands and options are completed without extra work. Effort S.

### G7. HTML charts

`cmon plot` writes PNGs and installs matplotlib on first use. `cmon plot --html` would produce one
interactive file with the same three views, plus those of C4 and C5. Effort M. A JS library loaded
from a CDN stops working offline; embedding it makes the file bigger.

### G8. Switchable views in `watch`

`watch` shows the windows and a burn line. Keys to switch between windows, clients, projects and
active sessions would make it the complete terminal dashboard, without leaving for `burn`.
Effort M. It holds DuckDB's write lock, so the new views must reuse its open connection.

## Planned elsewhere

All but usage-by-client are in Portuguese.

| Topic | Where | State |
|---|---|---|
| Codex as a second provider | [docs/plans/codex-provider.md](docs/plans/codex-provider.md) | In progress, partly built |
| Other clients: GLM, Aider, OpenCode, Gemini CLI | [docs/plans/MULTI_PROVIDER_PLAN.md](docs/plans/MULTI_PROVIDER_PLAN.md) | Plan |
| Automatic switch to z.ai when the limit runs out | [docs/plans/FALLBACK_PLAN.md](docs/plans/FALLBACK_PLAN.md) | Plan |
| More `burn --by` dimensions, alerts per client, weekly summary | [docs/plans/usage-by-client.md](docs/plans/usage-by-client.md), Backlog section | Backlog |

## Out of scope

- **A proxy in the request path.** It would give exact per-request measurement and switching
  providers mid-session, but it turns cmon from an observer into a point of failure for Claude
  Code. The fallback plan rules it out too.
- **Renewing Claude Code's token.** cmon 0.5 did, and logged users out; since 0.6 it only reads
  the token.
- **A central server for teams.** Sync through a shared folder exists precisely so there is no
  server, and files from another account are ignored on purpose.
- **A macOS widget.** It needs an Xcode project and an app extension, which ends the
  dependency-free `swiftc` build.
- **claude.ai chat usage per conversation.** There's no local log of it; it keeps showing up only
  as *other*.
