# Usage by client and by device

A user asked to see where their plan goes per client: the VS Code extension, the desktop app, the
terminal CLI, and so on. This plan covers what cmon can measure, what it can't, and the increments
that ship it.

## What already exists

Every assistant message in `~/.claude/projects/**/*.jsonl` carries an `entrypoint`, and
`scan_logs` stores it in `token_log.surface`. `cmon burn --by surface` already reports tokens and
API-equivalent US$ per client:

| entrypoint | Label | Client |
|---|---|---|
| `cli` | terminal | `claude` in a terminal |
| `claude-vscode` | vscode | VS Code extension |
| `claude-desktop` | app (desktop) | Claude Code sessions inside the Claude desktop app |
| `sdk-cli` | sdk (-p/agent) | `claude -p`, Agent SDK |

Unknown entrypoints pass through verbatim, so a new client shows up without a code change.

## What can't be measured

- **claude.ai chat** (web, desktop chat, mobile) writes no local transcript. It does count against
  the same weekly limit.
- **Other machines** keep their own `~/.claude/projects`.
- **Cloud sessions** (claude.ai/code) run remotely and leave nothing local.

All three still move the official percent. So the honest per-client answer has a remainder: the
part of the plan no local log explains.

## The gap

US$ is the wrong unit for a subscriber: they pay a flat fee, and what runs out is the percent of
the limit. Nothing today says "VS Code used 41 points of this week's 76%". And with two machines,
each one sees only half of the picture.

## Design

### Plan share: percent points per group

Snapshots (`collect` every 20 min, `watch` every tick) give the official percent over time. For
each pair of consecutive snapshots of one window, the increase `Δ` is split across groups in
proportion to the API-equivalent cost each group logged in that interval. Cost, not raw tokens,
because the limit weighs models differently and cache reads are cheap; the price table is the best
public proxy for that weighting.

- `Δ > 0` with no local cost in the interval goes to **other**: claude.ai chat, other devices,
  cloud sessions, other accounts.
- A change of `resets_at` (more than an hour apart; the API jitters by milliseconds) starts a new
  cycle; the first snapshot of a cycle contributes its own percent, split by the cost logged since
  the cycle started.
- Negative `Δ` inside a cycle (rounding) is dropped.
- Model-scoped windows (`weekly_scoped:Fable`) count only that model's cost.

It is an estimate: the percent is an integer, snapshots are minutes apart, and a chat message sent
while the terminal was busy lands on the terminal. Over a week those errors are small next to the
signal; the output says it's an estimate.

`burn --plan [WINDOW]` adds the share to any `--by` grouping (surface, project, model, day,
session, device). Without `--since` it covers the window's current cycle, which is the question
people actually ask ("who spent this week's 76%?"). `WINDOW` defaults to `weekly_all`.

### Drill-down filters

`burn --surface vscode --by project` answers "which projects did I run from VS Code". Accepts the
label or the raw entrypoint.

### Devices through a shared folder

There's no server, and there shouldn't be one. Devices exchange their own rows through a folder the
user already syncs (iCloud Drive, Dropbox, Syncthing, a network share):

```
<sync_dir>/<device_id>.parquet   # one file per machine, rewritten atomically
```

- `cmon config set sync_dir <path>` turns it on; `cmon config set device_name <name>` labels the
  machine (default: the short hostname). The device id is a random id kept in the DB's `meta`.
- `cmon sync` exports this machine's rows and imports the others'. `collect` runs it after each
  snapshot when `sync_dir` is set, best-effort: a sync failure never fails a collection.
- Only rows this machine read from its own transcripts are exported, never imported ones, so
  files don't echo between machines. Rows are those of `token_terminal`, already filtered to the
  terminal's account.
- Each file records the exporting account. A file from another account is skipped and reported:
  merging two accounts would attribute someone else's usage to this plan.
- Imported rows keep their `source_event_id` (message id + request id), so reimporting is
  idempotent and a file duplicated by the sync tool (`x 2.parquet`) is harmless.
- The device lands in `token_log.metadata_json` as `{"device_id": …}`; device names live in
  `meta` (`device:<id>`), refreshed on every import, so renaming a machine relabels its history.
  No schema change, and older cmon versions still read the database.
- Imported Desktop sessions are added to `session_account` under the terminal account (the
  exporter already proved they belong to it), so `token_terminal` includes them unchanged.
- The files carry project names and session ids. They stay in the user's own folder; the README
  says so.

`burn --by device` and `burn --device <name>` use it, and plan share's "other" bucket shrinks to
what really has no log anywhere.

## Increments

| ID | What | Proof |
|---|---|---|
| INC-1 | This document | — |
| INC-2 | `_plan_share()`: split Δ% per group by interval cost, "other" bucket, cycles, scoped windows | unit tests with synthetic snapshots + rows |
| INC-3 | `burn --plan [WINDOW]`: pp column and share in text and JSON; default to current cycle | CLI tests |
| INC-4 | `burn --surface X` filter (label or entrypoint) | CLI test |
| INC-5 | Device identity, `cmon sync` (export/import parquet, account guard), `--by device`, `--device` | tests with two simulated machines |
| INC-6 | `sync_dir`/`device_name` config keys; `collect` syncs when configured | test that collect exports |
| INC-7 | README, CHANGELOG, AGENTS.md | `make test` |

Order is linear: all of them edit `cmon.py`.

## Backlog: more dimensions the transcripts already carry

Each assistant line has fields cmon doesn't store yet. Each would be a new `--by` value, stored in
`metadata_json` for new rows (old rows would show `?` until rescanned while still on disk):

| Field | Question it answers |
|---|---|
| file under `<session>/subagents/` | How much went to subagents versus the main conversation |
| `attributionSkill` | Which skill burns the most |
| `attributionMcpServer` / `attributionMcpTool` | Which MCP server is expensive |
| `attributionPlugin` | Which plugin |
| `effort` | What high effort costs versus medium |
| `gitBranch` | Spend per branch, per feature |
| `version` | Did a Claude Code upgrade change the burn |
| `usage.speed` | What fast mode costs |
| `usage.server_tool_use` | Web search and fetch counts |

Other ideas, outside the logs:

- Per-client share in `status --json` and a `{client}` placeholder in the menu bar, from a cached
  share computed by `collect` (status must stay a cache read).
- Alerts per client: "VS Code took 30 pp today".
- A weekly digest: share by client, project and device, against the previous week.
