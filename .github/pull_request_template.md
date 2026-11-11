<!-- What this changes and why. Link the issue (Fixes #123) or the ROADMAP item (e.g. B5). -->

## Checklist

- [ ] `make test` passes on macOS, or `uv run ruff check . && uv run pytest -q` elsewhere
- [ ] A behaviour change comes with a test
- [ ] A user-facing change has an entry under `## [Unreleased]` in CHANGELOG.md, and README.md and
      AGENTS.md are updated where they describe that behaviour
- [ ] A database change migrates existing data (`_ensure_schema`)
- [ ] `status --json`, `doctor --json`, `burn --json` and the `guard` exit codes only gained fields
      or values; nothing an existing reader relies on changed
- [ ] The diff and the test fixtures hold no token, real usage data or personal paths
