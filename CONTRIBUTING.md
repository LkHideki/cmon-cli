# Contributing to cmon

Thanks for your interest. Bug reports, fixes, roadmap features and documentation are all welcome.
By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Where to start

- **Something broke:** open a [bug report](https://github.com/LkHideki/cmon-cli/issues/new/choose).
  It asks for `cmon doctor --json --offline`, which never shows the token.
- **Something to build:** [ROADMAP.md](ROADMAP.md) lists candidate features, each with the code it
  builds on and what can go wrong. Those with an issue carry the
  [`roadmap`](https://github.com/LkHideki/cmon-cli/labels/roadmap) label, and the smaller ones
  [`good first issue`](https://github.com/LkHideki/cmon-cli/labels/good%20first%20issue).
- **A security problem:** report it privately, as [SECURITY.md](SECURITY.md) explains.

Comment on the issue before starting anything bigger than a small fix, so that two people don't
build the same thing and the design can be settled before the code.

## Development environment

The project uses [uv](https://docs.astral.sh/uv/) and needs **Python 3.11+**.

```bash
git clone https://github.com/LkHideki/cmon-cli.git
cd cmon-cli
uv sync                  # lean environment
uv sync --extra plot     # also matplotlib and seaborn, for `cmon plot`
uv run cmon --help       # run straight from the source
```

`uv run cmon` uses your real login and writes its caches to your real `~/.cmon`. To try a command
that writes to the database, give it a scratch one: `uv run cmon --db /tmp/scratch.duckdb collect`.

| Path | What it is |
|---|---|
| [`cmon.py`](cmon.py) | The whole CLI, one module. Commands are functions (`now`, `collect`, `burn`…) wired to argparse in `main()` |
| [`menubar/`](menubar/) | cmon Bar, the macOS app, in Swift with no dependencies; `menubar/test.sh` runs its tests |
| [`scripts/setup.sh`](scripts/setup.sh) | What `make install`, `make uninstall` and `make doctor` run |
| [`tests/`](tests/) | pytest suite; [`tests/conftest.py`](tests/conftest.py) gives every test a temporary home and blocks the network |
| [`tests/golden/`](tests/golden/) | Reference outputs; when one changes, the diff should show why |
| [`docs/plans/`](docs/plans/) | Design plans for larger work, in Portuguese |
| [`AGENTS.md`](AGENTS.md) | The guide for AI coding assistants, also linked as `CLAUDE.md` |

## Before opening a PR

Run what CI runs. On macOS, `make test` does it all: ruff, pytest and the cmon Bar tests. On Linux
or Windows, run the first two:

```bash
uv run ruff check .        # lint; `--fix` corrects what it can
uv run pytest -q           # never touches your data or the network
uv run cmon --help         # smoke test
```

CI also runs the suite against DuckDB 1.1.1, the oldest version `pyproject.toml` accepts. To
reproduce that job, use a separate environment so your `.venv` stays as it is:

```bash
UV_PROJECT_ENVIRONMENT=.venv-duckdb11 uv sync
uv pip install --python .venv-duckdb11 duckdb==1.1.1   # `uv pip` ignores UV_PROJECT_ENVIRONMENT
UV_PROJECT_ENVIRONMENT=.venv-duckdb11 uv run --no-sync pytest -q
```

The ruff configuration lives in `pyproject.toml` (`line-length = 120`, rules `E/F/I/UP/B`).

## What a change needs

- **A test for every behaviour change.** Tests run against a temporary home with the network
  blocked, so they can't hit the API or your database.
- **A CHANGELOG entry for every user-facing change**, under `## [Unreleased]` at the top of
  [CHANGELOG.md](CHANGELOG.md), in Added, Changed or Fixed. Create the section if it isn't there.
  Say what the user sees, not how the code does it.
- **Docs that match.** README.md documents every command and option; AGENTS.md documents what an
  assistant reads (`status --json`, `doctor --json`, `guard`, `burn --json`).
- **Stable JSON.** `status --json`, `doctor --json`, `burn --json` and the `guard` exit codes are
  read by scripts, cmon Bar and assistants. Add fields; don't rename, retype or remove one.
- **Migrations, not breakage.** A database change must migrate existing data in
  `_ensure_schema`, in one transaction. Test it against a copy of a real database.
- **No secrets, ever.** Never print or log a token, not even in an error message (see
  `_without_token`). `.env`, `*.duckdb` and `*.png` are already in `.gitignore`; test fixtures
  must not hold real usage data either.
- **Lean dependencies.** A heavy new dependency goes in an optional extra, like `plot`.

Keep the change to one concern, and match the style of the code around it.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <summary in imperative, lowercase>

<optional body explaining the why, not the what>
```

Types used: `feat`, `fix`, `docs`, `perf`, `refactor`, `style`, `chore`, `ci`, `test`. Write in
clear English, use one type per commit (split a `perf` and a `feat` into two commits), and when a
commit changes behaviour, explain why in the body, with numbers where they help (`72s -> 1.3s`).

## Releases

Maintainers cut releases from `main`: the `## [Unreleased]` section becomes `## [X.Y.Z] — date`,
the version goes up in `pyproject.toml` and `uv.lock`, and a commit `release: X.Y.Z — title` gets
the tag `vX.Y.Z` and a GitHub release with the changelog section. Versions follow
[Semantic Versioning](https://semver.org/); a change that needs the user to do something gets an
*Upgrading* note in its section.
