# Axos — File Guide & How to Run

One entry per file: what it is for, its current state, and how it is meant to be run.

> **Honest status:** the project does **not run yet**. 16 of 17 Python files are empty
> placeholders, and the one real file (`llm.py`) fails at import. See
> [docs/ARCHITECTURE.md §4](ARCHITECTURE.md) for the full defect list. "How to run"
> below gives the intended commands *plus* the minimum work needed to make each one real.

---

## Runtime prerequisites

| Requirement | Value | Status |
|---|---|---|
| Python | ≥ 3.10 (`.python-version` pins 3.10) | ✅ configured |
| Package manager | [uv](https://docs.astral.sh/uv/) (`uv.lock`, `uv_build` backend present) | ✅ configured |
| `openai` SDK | imported by `llm.py` | ❌ **not declared** — add to `pyproject.toml` → `dependencies = ["openai"]`, then `uv sync` |
| API access | `config.BASE_URL` / `config.API_KEY` / `config.MODEL` | ❌ `config.py` is empty |

## Project root files

### `pyproject.toml`
Package metadata, build system (`uv_build`), console-script entry point, Python floor.
**Action needed:** entry point `axos = "axos:main"` points at a nonexistent module;
declare `openai`; align the package name (see ARCHITECTURE.md §5).

### `README.md`
Currently a single title line. No usage documented yet.

### `uv.lock`
Locked dependency resolution for uv. Regenerate with `uv lock` after editing `pyproject.toml`.

### `.python-version`
Pins the interpreter to **3.10** for uv/pyenv auto-selection. Nothing to run.

### `.gitignore`
Standard Python ignores. Nothing to run.

## `Axoscode/` — the package

State legend: ✅ implemented · 🟤 empty placeholder

### `llm.py` ✅ *(only implemented file)*
**What it's for:** the LLM gateway. Creates one OpenAI-compatible client from
`config.BASE_URL/API_KEY`, exposes `call_llm(messages, tools)` which sends the
conversation with `tool_choice="auto"`, and returns the assistant message plus a
token-usage dict (prompt / completion / reasoning / cached / total). Also forwards
`compact_at` / `compact_to` compaction knobs and contains a `__main__` demo REPL.
**How to run it (intended):**
```bash
uv run python -m Axoscode.llm        # starts the demo: prompts you, calls the model, prints usage
```
**Will currently fail** with `ImportError` (`config.BASE_URL`, `tools.TOOL_SCHEMAS`)
and has bugs at lines 27, 35–36, 55. Fix config.py + tools.py + those bugs first.

### `config.py` 🟤
**Intended:** single source of settings — `BASE_URL`, `API_KEY` (from env), `MODEL`,
`CONTEXT_WINDOW`, `COMPACT_AT`, `COMPACT_TO`. Everything else imports this.
**Critical path:** until it has these 6 constants, *nothing in the project imports successfully*.

### `tools.py` 🟤
**Intended:** the tool layer — `TOOL_SCHEMAS` (JSON-schema descriptions sent to the
model) and a `TOOLS` name→handler registry (read/write/edit/bash-style primitives).

### `agent.py` 🟤
**Intended:** the core loop — call `llm.call_llm`, execute requested tool calls,
feed results back, repeat until the model answers. Should eventually host `main()`
for the `axos` CLI entry point.

### `ui.py` 🟤
**Intended:** terminal front-end — streaming output, input reading, spinners/colour.
The user's eventual entry point via `uv run axos`.

### `prompt.py` 🟤
**Intended:** build the system prompt (currently `SYSTEM_PROMPT` is a blank string
inside `llm.py` — that constant logically belongs here).

### `context.py` 🟤
**Intended:** assemble model context — working directory info, file state, injected
instructions — into messages.

### `history.py` 🟤
**Intended:** conversation transcript management (append, serialize, trim).

### `compact.py` 🟤
**Intended:** context compression — summarize old turns when approaching
`CONTEXT_WINDOW`. Ties directly to the `compact_at`/`compact_to` params already
being passed in `llm.py`.

### `session.py` 🟤
**Intended:** session lifecycle — persist/resume transcripts to disk.

### `commands.py` 🟤
**Intended:** slash-command handling in the REPL (`/help`, `/clear`, `/compact`, etc.).

### `permissions.py` 🟤
**Intended:** gate each tool call (auto-allow reads, confirm writes/bash, remember choices).

### `sandbox.py` 🟤
**Intended:** isolate tool execution — working-directory confinement, timeouts for shell commands.

### `skills.py` 🟤
**Intended:** discover and load skill/instruction packs to extend the agent's knowledge.

### `subagent.py` 🟤
**Intended:** spawn a nested agent loop as a tool (parallel/isolated sub-task execution).

### `todos.py` 🟤
**Intended:** lightweight task list the agent maintains to track multi-step work.

---

## How to run — current vs intended

### Today (nothing works end-to-end)
The only command that even reaches Python is the module demo, and it dies on import:
```bash
cd Axos
uv run python -m Axoscode.llm
# ModuleNotFoundError: No module named 'openai'   ← fix: add openai to dependencies, uv sync
# ImportError: cannot import name 'BASE_URL' from 'Axoscode.config'  ← fix: implement config.py
# ImportError: cannot import name 'TOOL_SCHEMAS' from 'Axoscode.tools' ← fix: implement tools.py
```

### Minimum path to a first successful LLM call
1. `pyproject.toml`: `dependencies = ["openai"]` → run `uv sync`
2. Implement `Axoscode/config.py`:
   ```python
   import os
   BASE_URL = os.environ.get("AXOS_BASE_URL", "https://api.openai.com/v1")
   API_KEY  = os.environ.get("AXOS_API_KEY", "")
   MODEL    = os.environ.get("AXOS_MODEL", "gpt-4o-mini")
   CONTEXT_WINDOW = 128_000
   COMPACT_AT     = 100_000   # remove these two until llm.py stops passing them to the SDK
   COMPACT_TO     =  50_000
   ```
3. Implement `Axoscode/tools.py` with at least `TOOL_SCHEMAS = []` and `TOOLS = {}`
4. Fix `llm.py` bugs: line 27 (`message = response.choices[0].message`), remove the
   non-SDK kwargs or guard them, import `TOOLS` in `__main__`, fix `resut` typo
5. Run: `AXOS_API_KEY=sk-... uv run python -m Axoscode.llm`

### Eventually (once `agent.py`/`ui.py` exist)
```bash
uv sync                # install deps
uv run axos            # the intended full agent CLI
# or, for hack-free local use without installing:
uv run python -m Axoscode.agent
```

### Running as tests / checks
No test suite exists yet. When code lands, the natural split mirrors the scaffold:
pure modules (`config`, `prompt`, `compact`, `todos`) unit-test directly;
`llm`/`tools`/`sandbox` need mocked clients or temp directories.
