## What and why

<!-- What does this change and why? Link issues with "Fixes #123". -->

## How it was verified

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run pytest`
- [ ] `bash scripts/ci_e2e.sh` (UI / server changes)
- [ ] Model change: equations documented in `docs/MODEL.md`, conservation still closes, filesets re-settled if parameters changed
- [ ] Bug fix: regression test added and `docs/VALIDATION.md` updated
