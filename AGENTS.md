# cmon: guide for AI coding assistants

cmon tracks Claude plan limits. It is a CLI (`cmon.py`) plus, on macOS, a menu bar app (`menubar/`).
This file is also what `CLAUDE.md` points to.

## Installing it for a user

Tell the user up front that this adds a command, a menu bar app and a background job, all at user
level, and that macOS may show a Keychain prompt they need to answer.

### macOS 14 or newer: the full setup

```sh
git clone https://github.com/LkHideki/cmon-cli && cd cmon-cli
make install
make doctor   # exit 0 means everything works
```

`make install` prints the exact command for a missing prerequisite and stops. Ask the user before
running either one, then run `make install` again:

| Missing | Command | Note |
|---|---|---|
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | Needs a new shell afterwards |
| Swift | `xcode-select --install` | Opens a system dialog only the user can complete |

`make install` can exit 0 with checks still failing, so always judge the result by `make doctor`.
Each failing check prints its fix:

| Check | What to do |
|---|---|
| `usage API reachable and logged in` | The user must log in to Claude Code, or run `cmon token set`. Don't try to do this for them. |
| `cmon Bar running` | `open ~/Applications/"cmon Bar.app"` |
| `bar.json is valid` | Fix `~/.cmon/bar.json`, or delete it to get the defaults |
| Anything else | `make install` again; it is safe to repeat |

### Linux, Windows, or the CLI alone

```sh
uv tool install git+https://github.com/LkHideki/cmon-cli
cmon now       # check that it reads the usage
cmon install   # optional: background collection (--dry-run shows what it would do)
```

cmon Bar is macOS only. cmon isn't on PyPI; the PyPI package named `cmon` is an unrelated project.

### What the setup changes

| Location | What | Removed by `make uninstall` |
|---|---|---|
| `uv tool dir --bin`, usually `~/.local/bin/cmon` | The command | Yes |
| `~/Applications/cmon Bar.app` | The app | Yes |
| `~/Library/LaunchAgents/com.cmon.bar.plist` | Starts the app at login | Yes |
| `~/Library/LaunchAgents/com.cmon.collect.plist` | Collects usage every 20 min | Yes |
| Shell startup file | Adds uv's bin directory to `PATH`, if it was missing | No |
| `~/.cmon/` | History, settings, cache | No; the user decides whether to delete it |

## Rules when operating cmon

- Never print or copy a token, and don't read Claude Code's credentials yourself. `cmon token status`
  shows the source with the token masked.
- Don't edit `~/.cmon/usage.duckdb` by hand. Newer cmon versions migrate it on first use, in one
  transaction, and older versions can't read the result: if the user may go back, copy it first.
- `cmon watch` and `cmon collect` hold DuckDB's write lock. Read commands degrade rather than fail.
- Ask before setting `sync_dir`: it makes cmon write project names and session ids into that folder
  every 20 minutes, and the folder is usually synced to a cloud service.

## Reading the data

Prefer `cmon status --json`, the stable contract (`--live` forces an API call):

```json
{"version": 1, "offline": false, "observed_at": "2026-11-03T21:17:07+00:00", "age_s": 240,
 "providers": {"claude": {"windows": [
   {"key": "session", "label": "Current session", "percent": 5.0,
    "resets_at": "2026-11-04T02:00:00+00:00", "active": false},
   {"key": "weekly_all", "label": "All models", "percent": 69.0,
    "resets_at": "2026-11-04T14:00:00+00:00", "active": true}]}}}
```

A window may also carry `by_client` (`clients[]` with `key`, `label`, `share` as a fraction of
what the window used, plus `other`), written by `cmon collect` for the current cycle; it's absent
until collection has run.

`offline: true` means no data is available, and `providers` is then empty. `status` exits 0 even
when offline, so check that field rather than the exit code.

For where the percent went, use `cmon burn --by surface --plan --json` (also `--by project`,
`model`, `day`, `device`): `groups[].plan_pp` is each group's points of the window's percent over
the current cycle, and `other_pp` is what rose with nothing logged on this machine (claude.ai chat,
other devices). It's an estimate built from snapshots and local transcripts.

## Explaining the menu bar

The default title `90% 75'  44% 91` has two groups: the 5h window, then the weekly limit, each as
percent used and time until reset. Plain numbers are hours, `'` is minutes, `"` is seconds.

To change it, edit `segments` and `separator` in `~/.cmon/bar.json`; the app rereads the file on
each refresh. The placeholders are listed in the README's Menu bar section.

## Changing the code

Read [CONTRIBUTING.md](CONTRIBUTING.md). Run `make test` before committing. Tests run against a
temporary home with the network blocked, enforced by `tests/conftest.py`. The Codex provider is
only partly built: see [docs/plans/codex-provider.md](docs/plans/codex-provider.md).
