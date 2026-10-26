# Repository Guidelines

## Project Structure & Module Organization

Root stages are `scan.py`, `describe.py`, `embed.py`, `search.py`, and `gui.py`. `memes.py` aggregates commands; `common.py` handles configuration, SQLite, and embeddings. Each stage exposes `add_arguments(parser)`, `run(args, cfg)`, and `main(argv=None)`.

`schema.sql` defines storage; `description_schema.json` defines model output. Prompts live in `prompts/`, comparison queries in `sql/`, tooling in `scripts/`, and images in configured `image_dir`. [ARCHITECTURE.md](ARCHITECTURE.md) includes historical notes; check implementation claims against code and schemas.

## Build, Test, and Development Commands

1. `uv sync --locked` — install dependencies without changing `uv.lock`; requires Python 3.12+ and includes Dear PyGui.
2. Create `config.test.toml` from `config.example.toml`; set separate test database/image paths and backend settings.
3. `uv run memes.py all --config config.test.toml --limit 1` — scan everything in the sample directory; cap describe and embed independently.
4. `uv run memes.py search --config config.test.toml "widening the belt"` — print matching paths.
5. `uv run memes.py gui --config config.test.toml` — open the GUI.
6. `ruff format .` followed by `ruff check .` — required after Python edits. Ruff must be available separately; `uv sync` does not install it.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `UPPER_SNAKE_CASE` constants, and type hints. Prefer small functions and stdlib; ask before adding dependencies. HTTP calls use `urllib`.

Search descriptions using `bge-m3` and FTS; preserve resumability and stored work. For prompt changes, read [prompts/guidelines.md](prompts/guidelines.md), preserve the `vcodex1-master` baseline, and create versioned candidates. Bump `prompt_version` when changing the schema.

## Testing Guidelines

No automated suite or coverage target is committed. Follow [TESTING.md](TESTING.md) on copied images; verify repeat runs, retries, duplicates, and affected GUI behavior. Exclude prompt-example hash prefixes `045f3bf5`, `0be81bf3`, `b8bbfce6`, and `6e7aa3c5` from quality comparisons.

GUI deletion and background embedding are implemented despite older deferred-feature notes. GUI embedding uses the newest successful description across configurations; CLI embedding stays within the active prompt.

## Commit & Pull Request Guidelines

Use concise imperative subjects; history also uses `docs:` and `gui:` prefixes. Keep commits focused. PRs should explain behavior and validation, link relevant issues, and show screenshots for GUI changes.

## Configuration & Data Safety

Describe supports `openai` HTTP, `claude-code-cli`, `codex-cli`, and `opencode-cli`; configure embeddings independently. The default local setup uses Ollama with `qwen3-vl:8b` and `bge-m3`; CLI backends require authenticated clients. FFmpeg handles animations. Keep credentials, databases, and personal images uncommitted. Deletion requires confirmation. File deletion/move code must verify SHA-256 immediately before acting and preserve records on filesystem failure.
