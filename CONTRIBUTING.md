# Contributing to Pulse

Thanks for opening this file. Pulse is an empathic-memory engine for AI
companions; contributions that make retrieval more honest, more
explainable, or more truthful are welcome. Issues and PRs are both fine
entry points.

## Quick start

```bash
git clone git@github.com:nikshilov/pulse.git
cd pulse
make build       # compile the Go server -> bin/pulse
make test        # run Go + Python suites
make run         # start the server on 127.0.0.1:18789
```

See [`examples/`](./examples/) for runnable end-to-end demos.

## Dev environment

- **Go** 1.22+ (the module declares 1.25 in `go.mod` for forward compat)
- **Python** 3.11+ for `scripts/` and `scripts/tests/`
- **SQLite** comes bundled via `modernc.org/sqlite` (no system dep)
- **API keys** (only for the extraction pipeline, not core retrieval):
  - `ANTHROPIC_API_KEY` — Sonnet triage + Opus extract
  - `OPENAI_API_KEY` — `text-embedding-3-large` (3072-d)

Install Python dev deps:

```bash
make deps        # pip install -r requirements.txt + pytest
```

## Running tests

```bash
make test        # Go (./...) + Python (scripts/tests/)
make test-go
make test-py
```

Go tests use in-memory or `t.TempDir()` SQLite databases. Python tests
under `scripts/tests/` cover extraction, retrieval, consolidation, and
graph operations (348 tests as of writing).

CI expectation: every PR should pass `make test` cleanly.

## Code style

### Go

- `gofmt` for everything: `make fmt`
- `go vet ./...` clean: included in `make lint`
- Errors wrapped with `fmt.Errorf("context: %w", err)` rather than
  unwrapped pass-through
- Public types and exported funcs get short doc comments
  (`// Foo does X.` style)
- Tests live next to code (`foo.go` + `foo_test.go`)

### Python

- Roughly black-formatted (line ~100). The repo does not enforce a
  formatter yet; match the existing style in the file you're editing.
- Type hints encouraged for new functions
- Tests under `scripts/tests/test_*.py`, `pytest`-discoverable

### SQL migrations

- New migrations live under `internal/store/migrations/NNN_name.sql`
- Numbering is sequential; never edit a shipped migration
- Add a corresponding test (Python or Go) that opens a fresh DB and
  exercises the new schema

## Submitting a PR

1. Branch from `main` (`git switch -c feat/short-name`)
2. Keep changes focused — one feature or fix per PR
3. Add or update tests for behaviour changes
4. Run `make test && make lint` locally before pushing
5. Open the PR with a description that answers:
   - **What** the change does
   - **Why** (link to issue or describe motivation)
   - **How to verify** (commands, expected output, screenshots)
6. Avoid force-push after review starts; rebase/squash at the end

Small commits with descriptive messages help review. Match the existing
commit style: `feat(retrieval): v3 Phase 5.5 — emotion-hint ...`

## Areas where contributions are welcome

- **External benchmarks** — wiring Pulse v3 into LongMemEval / LoCoMo /
  ES-MemEval reproducibly (current numbers in README)
- **Provider adapters** — new ingest sources beyond `claude-jsonl`
  (Telegram archives, Limitless, Markdown notes, etc.)
- **MCP server** — `retrieve_memory` tool exposure (in flight; see
  [`mcp/`](./mcp/))
- **Docs** — examples, walkthroughs, integration recipes

If you have an idea that isn't on this list, open an issue first so we
can discuss scope before you sink time into it.

## Reporting issues

Use the GitHub issue tracker:
[github.com/nikshilov/pulse/issues](https://github.com/nikshilov/pulse/issues).
Helpful issues include:

- A minimal repro (commands, fixture, expected vs actual output)
- Pulse version (`git rev-parse --short HEAD`)
- Go and Python versions (`go version`, `python3 --version`)
- Relevant log lines (server logs are JSON, easy to grep)

For security-sensitive reports, please email rather than file a public
issue. Contact info on the repo profile.

## Code of conduct

Be respectful. Disagree about ideas, not people. Pulse exists because
honest disagreement makes retrieval better — apply the same standard to
each other.

Concretely:
- No personal attacks, harassment, or discrimination
- Critique focuses on the code/design, not the contributor
- When you're wrong, say so; when you're not sure, ask

## License

Pulse is released under the [MIT license](./LICENSE). By submitting a
PR you agree that your contribution is licensed under the same terms.
