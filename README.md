# AI Token Firewall and Backtracker (codename: TraceGate)

An experimental proxy layer for LLM coding sessions. It estimates token and
API cost before a run starts, enforces a budget ceiling mid-stream, and can
rewind a session to any earlier step by forking a DAG of recorded events
(thoughts, tool calls, tool results, output).

Built for Anthropic Claude, Google Gemini, and OpenAI, with multi-turn tool
calling and Assistant-Prefill continuation (resume mid-sentence after a
budget pause instead of regenerating).

## Status

- Session backtracking (DAG trace + counterfactual fork): implemented.
- Token/cost prediction (pre-flight estimates): in progress.
- **Not yet run end-to-end.** This code was recovered from an AI chat export
  and has not been executed as a whole. Expect wiring gaps (see below).

## Known gaps (please read before running)

- `tracegate_mcp.py` (the FastMCP server, SQLite DAG store, and pre-flight
  estimator) is imported by every module but is **not included** here yet.
- `index.html` (the web UI) is referenced by the Dockerfile but is not
  included yet.
- `tracegate_bridge.py` is missing its FastAPI app, WebSocket endpoints, and
  the `MANAGER` connection manager; the streaming, tool-loop, and pipeline
  logic is present. Its `MultiProviderStreamRouter` also lacks the
  `_stream_anthropic` / `_stream_gemini` / `_stream_openai` methods (they only
  exist in `llm_stream_router.py`), so the bridge router cannot stream yet.
- `CONTRIBUTING.md` was recovered from a PDF export and is partially
  garbled; treat it as a rough draft.

## Bugs found by the test suite (tracked as `xfail`)

1. `llm_stream_router._stream_gemini`: when a Gemini function-call part has
   `id=None`, `getattr(fn, "id", fallback)` returns `None` instead of the
   `call_<name>` fallback, so the tool_call_id is lost.
2. `tracegate_universal.GitShadowBacktracker`: `capture_snapshot` runs
   `git add -A` with no exclusions, so the shadow tree captures the TraceGate
   SQLite DB (and `.tracegate/` state) itself; `restore_snapshot` then
   rewinds the DAG database and erases the nodes a rewind needs. The shadow
   index should exclude the DB and `.tracegate/`.
3. `tracegate_bridge.MultiProviderStreamRouter.stream_turn` dispatches to
   provider stream methods the class does not define (see Known gaps).

## Install

```bash
# Python 3.11+
uv pip install -e ".[dev]"   # or: pip install -e ".[dev]"
cp .env.example .env          # then fill in your keys
```

Environment variables (see `.env.example`):

- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `GITHUB_TOKEN` (optional, for indexing GitHub SKILL.md repos)
- `TRACEGATE_USD_TO_INR` (optional, cost display)

## Tests

```bash
pip install -e ".[dev]"
pytest -v
```

The suite has 181 tests: the original agentic-loop suite plus unit and
pipeline tests for the stream router, bridge formatters, tool registry,
budget-gated pipelines, universal/git-shadow backtracker, and CLI.

Current result: **178 passed, 3 xfailed** (the three bugs listed above are
marked `xfail` with reasons; no failures are hidden).

Because the real `tracegate_mcp.py` is still missing, the suite runs against
`tests/stubs/tracegate_mcp.py`, an offline stub that re-implements its API
(SQLite DAG store, pre-flight estimator, fork/top-up logic) so the rest of
the code can be tested for real. All LLM provider calls are mocked; no API
keys or network are used. Drop the real module at the repo root and it takes
precedence over the stub automatically.

## What's inside

| File | What it does |
| --- | --- |
| `llm_stream_router.py` | Multi-provider async streaming engine: normalized thought/tool/text stream events, per-provider Assistant-Prefill resume, first version of the budget-gated pipeline |
| `tracegate_bridge.py` | Extended version: tool registry with cross-provider schema translation, multi-turn tool execution loop, post-tool budget firewall, recursive pipeline state machine |
| `tracegate_cli.py` | `tracegate` CLI: `up`, `mcp`, `sync`, `estimate`, `tree`, `test` |
| `tracegate_universal.py` | Drop-in plugin installer for coding tools (OpenCode, Claude Code, Codex, Cursor, etc.) plus git shadow-tree snapshots and session start/end reports |
| `test_tracegate_agentic_loop.py` | Original pytest suite: schema compilation, tool replay, budget interrupts, DAG forking, prefill continuation |
| `tests/` | Extended test suite (stream router, formatters, tool registry, pipelines, universal/git-shadow, CLI) plus `tests/stubs/tracegate_mcp.py`, the offline stub for the missing core module |
| `conftest.py` | Pytest bootstrap: puts the stub on `sys.path` and supplies the missing `MANAGER` / `configure_sqlite_wal` for tests |
| `push_repo.py` | Helper to git-init and push this project to your own GitHub repo |
| `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore` | Packaging and run config |
| `.github/` | CI workflow, issue templates, PR template |

## License

MIT - see [LICENSE](LICENSE).
