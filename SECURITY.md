# Security policy

## Reporting a vulnerability

Report it privately through GitHub:
[open a security advisory](https://github.com/LkHideki/cmon-cli/security/advisories/new) (the
*Security* tab, then *Report a vulnerability*). Don't open a public issue, and don't put a working
token in the report; a masked one, or the output of `cmon token status`, shows the problem just as
well.

The conversation stays in the advisory until a fix is out. Fixes ship in a release whose
CHANGELOG entry credits you, unless you'd rather not be named.

## Supported versions

Only the latest release gets fixes. cmon has no long-term branches: upgrading is
`git pull && make install`, or `uv tool upgrade cmon-cli`.

## What cmon protects

These are the parts where a bug is a security bug rather than a plain one:

- **The OAuth token.** cmon takes it from `CLAUDE_OAUTH_TOKEN`, from the OS vault (`cmon token
  set`) or, read-only, from Claude Code's own login. It sends the token to
  `https://claude.ai/api/oauth/usage` and nowhere else, ignoring proxy and netrc settings from the
  environment. It must never show up in output, logs, caches, the database or error messages, and
  never be written in clear text unless `CMON_ALLOW_PLAINTEXT_KEYRING=1` says so.
- **Claude Code's login.** cmon never redeems or stores Claude Code's refresh token; doing so would
  log Claude Code out.
- **Usage data.** `~/.cmon` holds project names, session ids and costs. Only `sync_dir`, which the
  user sets, copies them anywhere else.
- **Commands cmon runs.** `CMON_HOOK` runs a command the user configured, with alert data in
  environment variables. cmon reads settings from `~/.cmon/.env` and never from a `.env` in the
  current directory, so a cloned project can't set the hook or swap the token.

Out of scope: the private usage endpoint changing or going away, and anything that requires an
attacker who can already write to `~/.cmon` or read your vault.
