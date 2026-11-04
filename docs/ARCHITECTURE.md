# Axos — Architecture

> **Project status: SKELETON.** As of 2026-11-04, 1 of 17 modules contains code
> (`llm.py`, ~57 lines). The remaining 16 are empty placeholder files created as a
> scaffold. This document describes (A) what is actually implemented today and
> (B) the intended architecture inferred from the module layout and `llm.py`.
> Sections marked *[planned]* do not exist in code yet.

---

## 1. What Axos Is

Axos is intended to be a **terminal-based coding agent** (in the style of Claude Code /
Codex CLI): a `uv`-managed Python package with zero third-party dependencies declared,
a single LLM gateway module, and a set of modules that together form a
tool-use agent loop over an OpenAI-compatible chat-completions API.

## 2. Current Reality (what exists)

### 2.1 Module map

```
Axos/
├── pyproject.toml          # package metadata, uv_build backend, entry point (broken — see §4)
├── README.md               # one line, title only
├── uv.lock / .python-version (3.10) / .gitignore
└── Axoscode/               # the package (17 modules, 16 empty)
    ├── llm.py              # ← ONLY implemented module
    ├── agent.py            # empty  (core loop)
    ├── config.py           # empty  (settings/constants)
    ├── context.py          # empty  (context assembly)
    ├── prompt.py           # empty  (system prompt construction)
    ├── history.py          # empty  (message history)
    ├── compact.py          # empty  (context compression)
    ├── session.py          # empty  (session lifecycle/persistence)
    ├── commands.py         # empty  (slash commands)
    ├── tools.py            # empty  (tool registry + schemas)
    ├── permissions.py      # empty  (tool-call gating)
    ├── sandbox.py          # empty  (execution isolation)
    ├── skills.py           # empty  (skills loading)
    ├── subagent.py         # empty  (child-agent spawning)
    ├── todos.py            # empty  (task/todo state)
    └── ui.py               # empty  (terminal rendering)
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
- **Compaction parameters (`compact_at`, `COMPACT_AT`, `COMPACT_TO`) are forwarded
  to the API call itself** — these are *not* standard OpenAI SDK parameters. They
  suggest either a custom/compaction-aware backend, or (more likely) an aspirational
  design where server-side context compaction is desired. Empty `compact.py`
  corresponds to this feature.
- A `__main__` block demonstrates the intended agentic exchange: prompt → LLM →
  first tool call → execute → print usage.

### 2.3 Dependency / import graph (actual)

```
llm.py ──► config.py        (BASE_URL, API_KEY, MODEL, CONTEXT_WINDOW, COMPACT_AT, COMPACT_TO)
   │                ⇩ config.py is EMPTY ⇒ ImportError at load time
   └──► tools.py           (TOOL_SCHEMAS)
                    ⇩ tools.py is EMPTY ⇒ ImportError at load time
   Also: `from openai import OpenAI` — openai is NOT declared in pyproject.toml
```

**Nothing in the project currently imports or runs.** `llm.py` fails at import on
all three fronts.

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

**The loop *[planned]*:** `ui` receives input → `agent` builds messages from
`prompt` + `context` + `history` → calls `llm.call_llm` → if the response contains
tool calls, each is checked by `permissions`, executed by `tools` handlers inside
`sandbox` (a tool may itself be `subagent`, which recursively runs the loop) →
results appended to `history` → repeat until the model stops calling tools →
`session` persists the transcript → `ui` streams the answer.

**Cross-cutting concerns *[planned]*:**
- `compact` / `context`: summarize or evict old history when token usage approaches
  `CONTEXT_WINDOW`, triggered around `COMPACT_AT`, targeting `COMPACT_TO`
  (mirrors the parameters already passed in `llm.py`).
- `skills`: markdown/instruction packs injected into `prompt` on demand.
- `todos`: lightweight task list the model can read/write to track multi-step work.

## 4. Known Defects (must fix before anything runs)

| # | Location | Problem |
|---|----------|---------|
| 1 | `pyproject.toml:13` | Entry point `axos = "axos:main"` references module `axos` and function `main()` — neither exists. |
| 2 | `pyproject.toml:10` | `dependencies = []` but code imports `openai` — must be declared. |
| 3 | `Axoscode/config.py` | Empty, yet `llm.py` imports 6 constants from it ⇒ `ImportError`. |
| 4 | `Axoscode/tools.py` | Empty, yet `llm.py` imports `TOOL_SCHEMAS` ⇒ `ImportError`. |
| 5 | `llm.py:27` | `messages = messages.choices[0].message` — wrong variable; should be `message = response.choices[0].message` (`messages` is the input list and gets destroyed). |
| 6 | `llm.py:29-30` | `completion_details` / `prompt_details` computed then discarded (dead code). |
| 7 | `llm.py:35-36` | `usage.reasoning_tokens` / `usage.cached_tokens` are not standard fields on OpenAI SDK usage objects; will raise unless the backend provides them. |
| 8 | `llm.py:24-25` | `compact_at` / `compact_to` are not valid `chat.completions.create()` kwargs; `max_tokens=CONTEXT_WINDOW` misuses a context-window size as a completion cap. |
| 9 | `llm.py:55` | `TOOLS` used but never imported; typo `resut`; only `tool_calls[0]` handled (multi-call responses dropped); result never appended to messages. |
| 10 | naming | Repo is `Axos`, package is `Axoscode`, entry point expects `axos` — three different names; `uv_build` will likely refuse to find the package. |

## 5. Recommended Target Layout *[planned]*

For the documented `axos = "axos:main"` entry point to work under `uv_build` with a
flat layout, either rename `Axoscode/` → `axos/` (and update all imports), or change
the script to `axos = "Axoscode.agent:main"` once `agent.py` owns the loop and CLI.

## 6. Implementation Order Suggested by the Scaffold

1. `config.py` → unblocks `llm.py` imports
2. `tools.py` (`TOOL_SCHEMAS` + `TOOLS` registry, start with read/write/bash)
3. fix `llm.py` defects (§4)
4. `agent.py` core tool loop → 5. `ui.py` REPL → 6. `prompt.py`/`history.py`/`context.py`
→ 7. `permissions.py`/`sandbox.py` → 8. `compact.py`/`session.py` → 9. `commands.py`/`todos.py`/`skills.py`/`subagent.py`
