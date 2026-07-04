# Contributing to cmon

Thank you for your interest in contributing! This guide covers the essentials to get started.

## Development environment

The project uses [uv](https://docs.astral.sh/uv/) to manage dependencies and environments.

```bash
# Clone and enter the directory
git clone https://github.com/LkHideki/cmon.git
cd cmon

# Install dependencies (lean environment)
uv sync

# To work with graphs (the `plot` command), include the optional extra
uv sync --extra plot

# Run the CLI
uv run cmon --help
```

Requires **Python 3.11+**. CI validates on 3.11, 3.12, and 3.13.

## Before opening a PR

Run the same checks that CI runs:

```bash
# Lint (mandatory — CI fails if there are violations)
uv run ruff check .

# Automatic formatting of trivial fixes
uv run ruff check --fix .

# Smoke test (imports the module and validates the CLI, no network)
uv run cmon --help
```

The ruff configuration lives in `pyproject.toml` (`line-length = 120`, rules `E/F/I/UP/B`).

## Commit messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <summary in imperative, lowercase>

<optional body explaining the WHY, not the what>
```

Types used: `feat`, `fix`, `docs`, `perf`, `refactor`, `style`, `chore`, `ci`, `test`.

Guidelines:

- **Write in clear, grammatically correct English.** Avoid typos and abbreviations that obscure meaning.
- Prefer **one type per commit**. Avoid composite types like `perf+feat:` — split into two commits or pick the predominant one.
- The body is optional, but when the commit changes behavior, **explain the why** and include numbers when it makes sense (e.g., `72s -> 1.3s`).

## Scope and style

- The source code is a single module: [`cmon.py`](cmon.py). Keep changes cohesive with the existing style.
- Heavy new dependencies should be added as **optional extras** (like `plot`), not in core.
- Never version secrets. `.env`, `*.duckdb`, and `*.png` are already in `.gitignore`.

## Reporting bugs and ideas

Open an [issue](https://github.com/LkHideki/cmon/issues) describing what you expected, what happened, and how to reproduce it. For the CLI, include the exact command and relevant output (redact tokens).
