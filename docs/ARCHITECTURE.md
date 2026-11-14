# Axos — Architecture

> **Project status: PROTOTYPE.** As of 2026-11-04, the agentic loop is implemented
> (`agent.py` ~170 lines, `llm.py` ~80 lines core + ~180 lines streaming). The core
> modules (config, tools, session, context, permissions, sandbox, compact, history,
> commands, todos, skills, ui) are populated; the agent loop that ties them together
> is the newest addition. Sections marked *[planned]* do not exist in code yet.

---

## 1. What Axos Is

Axos is intended to be a **terminal-based coding agent** (in the style of Claude Code /
Codex CLI): a `uv`-managed Python package, a single LLM gateway module, and a set
of modules that together form a tool-use agent loop over an OpenAI-compatible
chat-completions API.

## 2. Current Reality (what exists)

### 2.1 Module map

```
Axos/
├── pyproject.toml          # package metadata, uv_build backend, entry point
├── README.md               # one line, title only
├── uv.lock / .python-version (3.10) / .gitignore
└── Axoscode/               # the package (17 modules, all populated)
    ├── __init__.py         # empty package marker
    ├── llm.py              # LLM gateway: sync call_llm() + async call_llm_stream()
    ├── agent.py            # ← core agentic loop (async, streaming, batching,
    │                       #    stuck-loop detection, end_turn)
    ├── config.py           # settings: BASE_URL, API_KEY, MODEL, COMPACT_AT, etc.
    ├── context.py          # per-turn env injection (time, git, todos, changes)
    ├── compact.py          # context compaction: needed()/render()/compact()
    ├── session.py          # JSONL transcript persistence under ~/.agents/sessions/
    ├── commands.py         # slash commands: /compact, /rewind, /sessions
    ├── tools.py            # tool registry: TOOL_SCHEMAS, TOOLS, execute(),
    │                       # partition_tool_calls(), execute_async()
    ├── permissions.py      # Bash gating: BASH_RULES allow/ask/deny, check()
    ├── sandbox.py          # shell isolation: seatbelt (macOS), bwrap (Linux)
    ├── skills.py           # SKILL.md discovery: find_skills(), read_skill()
    ├── todos.py            # todo checklist: MARKS, TODOS, write_todos(), TODO_SCHEMA
    ├── ui.py               # Rich terminal UI: UI singleton, banner, approve,
    │                       #    tool rendering, todos, usage, summary, compacted
    ├── subagent.py         # read-only child agent: task() with MAX_TURNS=12
    └── prompt.py           # prompt_toolkit REPL input: read() with history
```

### 2.2 `llm.py` — the only working surface

The only real code path is a thin, **partially buggy** wrapper around the OpenAI
Python SDK:

```
user input ──► call_llm(messages, tools=TOOL_SCHEMAS)
                    │  model / base_url / api_key from config.*
                    │  tool_choice="auto"
                    │  passes non-standard params: compact_at / compact_to
                    ▼
              response ──► (message, usage-dict)
```

Notable design signals inside `llm.py`:

- **Token-usage tracking is a first-class concern** — the returned `usage` dict
  captures prompt / completion / **reasoning** / **cached** / total tokens.
- **Compaction parameters** (`compact_at` / `COMPACT_AT`, `compact_to` / `COMPACT_TO`)
  are module-level constants read from `config`. They do not affect the API call
  directly; compaction is performed locally after each turn (see `compact.py`).
- `call_llm_stream` enables the agentic loop's streaming display and tool-call
  parsing without waiting for the full response.

### 2.3 Dependency / import graph (actual)

```
llm.py ──► config.py        (BASE_URL, API_KEY, MODEL, CONTEXT_WINDOW, COMPACT_AT, COMPACT_TO)
   │                ⇩ config.py is populated ⇐ Import OK at load time
   └──► tools.py           (TOOL_SCHEMAS, TOOLS)
   Also: `from openai import OpenAI, AsyncOpenAI` — openai>=1.40.0 declared

Nothing fails at import — all modules resolve provided `AXOS_API_KEY` is set.
```

## 3. Intended Architecture *[planned]*

The file names map cleanly onto the standard components of a coding-agent runtime:

```
                        ┌──────────────────────────────┐
                        │            ui.py             │  terminal render / input
                        └──────────────┬───────────────┘
                                       │
     commands.py (slash cmds) ──► ┌────▼─────────┐ ◄── skills.py (loadable instruction packs)
     prompt.py   (system prompt)──►  agent.py    │ ◄── todos.py  (plan/task state)
     context.py  (assembly) ─────►  CORE LOOP   │
     history.py  (transcript) ───► └────┬───────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
               session.py         llm.py ◄────── config.py
            (persist/resume)   (chat-completions  (model, keys,
                               gateway + usage)    compaction knobs)
                    │
                    ▼
                tools.py (registry: schemas + handlers)
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
  permissions.py  sandbox.py  subagent.py
  (gate each       (isolate     (spawn child
   tool call)      execution)    agent loop)
```

**The loop:** `ui` receives input → `agent` builds messages from
`prompt` + `context.reminder` + `history` → calls `llm.call_llm_stream`
→ SSE events yield text + tool calls → `tools.partition_tool_calls`
groups calls by concurrency safety → safe batches run via
`asyncio.gather` (`execute_async`, parallel) / unsafe run solo
→ `session.save` → `compact` when tokens exceed `CONTEXT_WINDOW * COMPACT_AT`
→ repeat until model returns final text (end_turn) or
stuck-loop detection triggers (same tool+args repeats ≥3 times).

**Cross-cutting concerns:**
- `compact` / `context`: summarize or evict old history when token usage
  approaches `CONTEXT_WINDOW`, triggered around `COMPACT_AT`, targeting `COMPACT_TO`.
- `skills`: markdown/instruction packs injected into `prompt` on demand.
- `todos`: lightweight task list the model can read/write to track multi-step work.

## 4. Known Defects (state as of 2026-11-04)

| # | Location | Problem | Status |
|---|----------|---------|--------|
| 1 | `pyproject.toml:13` | Entry point `axos = "axos:main"` references module `axos` and function `main()` — neither exists in flat `uv_build` layout. | **Present** — package folder is `Axoscode/`, script expects `axos`. Fix: change entry point to `axos = "Axoscode.agent:main"` or rename package. |
| 2 | `pyproject.toml:10` | Code imports `prompt_toolkit`, `yaml`, `rich` but only `openai>=1.40.0` is declared. | **Present** — `uv sync` will fail on missing deps. Add `prompt-toolkit`, `pyyaml`, `rich`. |
| 3 | `llm.py:51` (`__main__` block) | `TOOLS[tool_call.function.name](**args)` — `TOOLS` is a dict, but `__main__` demo calls it without the async streaming path being the primary entry. | **Fixed** — `llm.py` now has both sync `call_llm()` and async `call_llm_stream()`; `__main__` uses sync path with `TOOLS` populated. |
| 4 | `agent.py` | Core agent loop not implemented — stub `main()` raises `SystemExit`. | **Fixed** — `agent.py` now has full async loop with streaming, batching, stuck-loop detection, end_turn. |
| 5 | `compact.py` | No auto-compaction trigger in the agent loop. | **Present** — compaction is triggered manually via `/compact` command or when `history.estimate(messages) > CONTEXT_WINDOW * COMPACT_AT` in the agent loop. |
| 6 | `agent.py:MAX_TURNS` | No upper bound on turns could lead to infinite loops. | **Present** — `config.MAX_TURNS = 50` bounds the loop; stuck-loop detection (`STUCK_LOOP_THRESHOLD = 3`) forces exit sooner. |
| 7 | Naming | Repo `Axos`, package `Axoscode`, entry point `axos` — three different names. | **Present** — see defect #1. Consistency improvement needed. |

## 5. Recommended Target Layout *[planned]*

For the documented `axos = "axos:main"` entry point to work under `uv_build` with a
flat layout, either rename `Axoscode/` → `axos/` (and update all imports), or change
the script to `axos = "Axoscode.agent:main"` once `agent.py` owns the loop and CLI.

## 6. Implementation Order Suggested by the Scaffold

1. `config.py` → ✅ populated (unblocks `llm.py` imports)
2. `tools.py` ✅ populated (`TOOL_SCHEMAS`, `TOOLS`, `execute`, `partition_tool_calls`, `execute_async`)
3. `llm.py` ✅ populated (sync `call_llm()`, async `call_llm_stream()`, `_ClientProxy`, lazy clients)
4. `agent.py` ✅ core tool loop implemented (async, streaming, batching, stuck-loop, end_turn)
5. `ui.py` ✅ Rich terminal UI implemented (banner, approve, tool, todos, usage, summary, compacted)
6. `prompt.py` ✅ `prompt_toolkit` REPL input reader
7. `history.py` ✅ transcript window management (`spill`, `cap`, `strip`, `estimate`, `fit`)
8. `compact.py` ✅ context compaction (`needed`, `render`, `summarize`, `compact`)
9. `session.py` ✅ JSONL transcript persistence (`save`, `load`, `rewind`, `open_session`, `all_sessions`)
10. `commands.py` ✅ slash commands (`/compact`, `/rewind`, `/sessions`)
11. `permissions.py` ✅ Bash gating (`BASH_RULES`, `check`, `split_command`, `inside_project`)
12. `sandbox.py` ✅ shell isolation (`wrap`, `run`, `name`)
13. `skills.py` ✅ SKILL.md discovery (`find_skills`, `read_skill`, `skills_prompt`)
14. `todos.py` ✅ todo checklist (`MARKS`, `TODOS`, `write_todos`, `todos_prompt`, `TODO_SCHEMA`)
15. `subagent.py` ✅ read-only child agent (`task`, `MAX_TURNS=12`, `WITHHELD`)
