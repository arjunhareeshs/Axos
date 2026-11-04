# Axos — File Guide

Detailed map of every file: **what it does**, **which functions it exposes**, and **how it connects** to the rest of the agent.

> Axos is a terminal coding agent: user ↔ REPL ↔ agent loop ↔ LLM + tools.
> Package directory: `Axoscode/`. CLI name: `axos` (see `pyproject.toml`).

---

## 1. How the pieces fit together

```
                          ┌─────────────────────────────────────┐
                          │              ui.py                  │
                          │   banner · ask · agent · tool ·     │
                          │   approve · usage · todos · spinner │
                          └──────────────────┬──────────────────┘
                                             │
   prompt.py (typed input) ──────────────────┤
   commands.py (/rewind /sessions /compact) ─┤
                                             ▼
                          ┌─────────────────────────────────────┐
                          │             agent.py                │  ← CLI entry (main)
                          │         CORE TOOL LOOP              │
                          │  build msgs → call_llm → execute    │
                          │  tools → append results → repeat    │
                          └───────┬───────────────┬─────────────┘
                                  │               │
              ┌───────────────────┘               └──────────────────┐
              ▼                                                      ▼
     ┌────────────────┐                                   ┌──────────────────┐
     │    llm.py      │◄──────── config.py                │    tools.py      │
     │  call_llm()    │   BASE_URL API_KEY MODEL          │ schemas + TOOLS  │
     │  + usage dict  │   CONTEXT_WINDOW COMPACT_*        │ execute()        │
     └───────┬────────┘                                   └────────┬─────────┘
             │                                                     │
             │                              ┌──────────────────────┼──────────────┐
             │                              ▼                      ▼              ▼
             │                       permissions.py            sandbox.py    subagent.py
             │                       check() allow/ask/deny    run() wrap()  task()
             │                                                                     │
             │                              skills.py          todos.py            │
             │                              read_skill()       write_todos()       │
             │                                                                     │
             ▼                                                                     ▼
     ┌────────────────┐              history.py              context.py
     │  compact.py    │◄──────────── fit/cap/estimate        reminder()
     │  summarize +   │              strip/spill/sweep       git + todos notes
     │  rebuild msgs  │
     └───────┬────────┘
             │
             ▼
        session.py  — save / load / rewind JSONL transcripts under ~/.agents/sessions/
```

### One turn (intended)

1. **`ui.ask` / `prompt.read`** — user types a message (or a `/command`).
2. **`commands.handle`** — if it starts with `/`, handle locally and skip the model.
3. **`context.reminder`** — inject env, git branch, file-change notes, todo list.
4. **`history.fit`** — trim oversized tool outputs so the window still fits.
5. **`llm.call_llm`** — send messages + `TOOL_SCHEMAS`; get assistant message + usage.
6. If there are **tool calls**: for each call, **`tools.execute`** →
   `permissions.check` → maybe `ui.approve` → run handler (`bash` via `sandbox`,
   `read_file`, `write_file`, `str_replace`, `read_skill`, `write_todos`, or `task`).
7. Append tool results to history; **`session.save`**; loop until the model stops calling tools.
8. If usage crosses **`COMPACT_AT`**, **`compact.compact`** rewrites history to a summary + recent tail.

---

## 2. Project root

| File | Role |
|------|------|
| `pyproject.toml` | Package `axos`, deps (`openai`, `prompt-toolkit`, `pyyaml`, `rich`), script `axos = "axos.agent:main"`. |
| `README.md` | Project title / short intro. |
| `uv.lock` | Locked deps for `uv sync`. |
| `.python-version` | Pins Python **3.10**. |
| `.gitignore` | Ignores venv, caches, secrets, etc. |
| `docs/ARCHITECTURE.md` | High-level architecture + known defects. |
| `docs/FILE_GUIDE.md` | This file. |

**Note:** entry point says `axos.agent:main`, but the on-disk package folder is `Axoscode/`. Until those names match (rename package or fix the script), `uv run axos` will not resolve correctly.

---

## 3. `Axoscode/` — module-by-module

Status legend used below:

- **On disk** — saved in the repo as described
- **Designed / buffer** — full implementation present in the working editor; may still need saving
- **Stub / empty** — placeholder only

---

### `config.py` — settings

**Job:** single source of truth for API and context-window knobs. Every other module that talks to the model or budgets tokens imports this.

**Exports (disk):**

| Name | Meaning |
|------|---------|
| `BASE_URL` | OpenAI-compatible API base (`AXOS_BASE_URL`, default OpenAI) |
| `API_KEY` | API key (`AXOS_API_KEY`) |
| `MODEL` | Model id (`AXOS_MODEL`, default `gpt-4o-mini`) |
| `CONTEXT_WINDOW` | Max context size in tokens (128000) |
| `COMPACT_AT` | When to trigger compaction (token threshold) |
| `COMPACT_TO` | Target size after compaction |

**Connected to:** `llm.py`, `history.py` (`fit`), `compact.py` (`needed`, `compact`).

**Alt design (env file):** some drafts load `~/.agents/.env` with `Path.home() / ".agents" / ".env"` and `os.environ.setdefault`, then read `BASE_URL` / `API_KEY` / `MODEL`. Compaction knobs can also be ratios (`0.85` / `0.35`) instead of absolute token counts.

---

### `llm.py` — LLM gateway

**Job:** own the OpenAI client and one function that talks to the chat-completions API.

**Functions / symbols:**

| Name | What it does |
|------|----------------|
| `client` | Module-level `OpenAI(base_url=…, api_key=…)` |
| `SYSTEM_PROMPT` | Placeholder system string (real prompt should move to `prompt`/`agent`) |
| `call_llm(messages, tools=None)` | Calls `chat.completions.create` with `tools or TOOL_SCHEMAS`, `tool_choice="auto"`. Returns `(message, usage_dict)`. |
| `__main__` | Mini demo: one user prompt → print reply → run any tool calls via `TOOLS` → print usage |

**Usage dict keys:** `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `total_tokens`.

**Connected to:**

- **imports** `config`, `tools.TOOL_SCHEMAS` / `TOOLS`
- **imported by** `agent` (planned), `subagent.task`, `compact` (uses `client` for summarizer)

---

### `tools.py` — tool registry + execution

**Job:** define what the model *may* call (`TOOL_SCHEMAS`) and how those calls *run* (`TOOLS` + `execute`).

**Handlers:**

| Function | Does |
|----------|------|
| `bash(command)` | Runs shell via `sandbox.run`; caps output with `history.cap` |
| `read_file(path)` | Reads a file; caps long contents |
| `write_file(path, content)` | Creates/overwrites a file |
| `str_replace(path, old_str, new_str, allow_multi_edit=False)` | Exact text replace; errors if 0 matches or >1 without multi |
| `execute(tool_call)` | Parse JSON args → `permissions.check` → optional `ui.approve` → `TOOLS[name](**args)`. Never crashes the session; returns `(args, result_or_error_string)` |

**Also registers (via imports):**

- `read_skill` from `skills.py`
- `write_todos` + `TODO_SCHEMA` from `todos.py`
- `task` + `TASK_SCHEMA` from `subagent.py`

**Data:**

| Name | Role |
|------|------|
| `TOOL_SCHEMAS` | List of OpenAI function schemas sent to the model |
| `TOOLS` | `name → callable` map used at runtime |

**Connected to:** `sandbox`, `permissions`, `history`, `ui`, `skills`, `todos`, `subagent`; consumed by `llm.call_llm` and the agent/subagent loops.

**Disk today:** stub `TOOL_SCHEMAS = []`, `TOOLS = {}` until the full registry is saved.

---

### `agent.py` — core loop / CLI entry

**Job:** own `main()` for the `axos` console script and (eventually) the full agent loop.

**On disk today:**

```python
def main():
    raise SystemExit("…not implemented yet — run python -m Axoscode.llm…")
```

**Intended loop (not wired yet):**

`ui` → build messages (`system` + history + `context.reminder`) → `call_llm` → `tools.execute` for each call → append → `session.save` → maybe `compact` → repeat until final text answer.

**Connected to:** almost everything; this is the hub.

---

### `ui.py` — terminal front-end (Rich)

**Job:** everything the human sees and types approvals for. Singleton: `ui = UI()`.

**Class `UI` methods:**

| Method | Role |
|--------|------|
| `banner(sandbox_name)` | Startup rule + sandbox hint |
| `clear` / `resumed` / `replay` | Clear screen; show resume label; redraw a saved transcript |
| `approve(reason)` | y/n gate used by `tools.execute` when policy is `ask` |
| `note` / `pick` / `ask` | Soft messages; numbered chooser; read next user line |
| `user` / `agent` | Render user text / markdown agent reply |
| `tool(name, args, result, nested=False)` | Panel for a tool call (or todos checklist) |
| `subagent(description)` | Panel for nested exploration (human-only) |
| `injection` / `debug` | Late system notes / raw JSON dump |
| `working(label)` | Spinner context manager while waiting on the model |
| `usage` / `summary` | Per-turn token line; end-of-session totals table |
| `compacted(before, messages)` | Show compaction handoff panel |
| `todos(todos)` | Pretty checklist from `write_todos` |

**Connected to:** `prompt.read` for input; `todos.MARKS` for checklist glyphs; used by `tools.execute`, `commands`, `subagent`.

---

### `prompt.py` — line editor (not the system prompt)

**Job:** multi-line capable REPL input via `prompt_toolkit` (history file, keybindings). Name is easy to confuse with “system prompt” — here it means **user input prompt**.

| Symbol | Role |
|--------|------|
| `HISTORY` | `~/.agents/history` |
| `read(prompt="> ")` | Read one message; EOFError on Ctrl-D |
| keybindings | Opt/Esc-left/right = word jump; Opt-Enter = newline |

**Connected to:** `ui.ask`, `ui.approve`, `ui.pick`.

---

### `permissions.py` — human gate for risky tools

**Job:** decide `allow` / `ask` / `deny` *before* a tool runs. Sandbox limits what is possible; this limits what is automatic.

| Function | Role |
|----------|------|
| `split_command(command)` | Split compound bash on `\| & ;` without breaking quoted strings |
| `decide(command)` | Match each part against `BASH_RULES` (fnmatch); strictest wins |
| `inside_project(path)` | True if path is under cwd project root |
| `check(name, args)` | Entry used by `tools.execute` → `(action, reason)` |

**Policy sketch:**

- Read-ish bash (`ls`, `rg`, `git status`, …) → **allow**
- Default bash → **ask**
- `rm`, `sudo`, `curl`, `git push`, … → **deny**
- `write_file` / `str_replace` outside project → **ask**
- Everything else → **allow**

**Connected to:** `tools.execute` only.

---

### `sandbox.py` — OS isolation for `bash`

**Job:** wrap shell commands so they cannot freely network or write outside the project.

| Function | Role |
|----------|------|
| `wrap(command)` | macOS → `sandbox-exec` seatbelt profile; Linux → `bwrap` if installed; else `None` |
| `name()` | `"seatbelt"` / `"bubblewrap"` / `"none"` (shown in UI banner) |
| `run(command, timeout=60)` | `subprocess.run` with wrap + capture stdout/stderr |

**Connected to:** `tools.bash`; `commands` / `ui.banner` via `sandbox.name()`.

---

### `history.py` — keep the transcript inside the window

**Job:** cap huge tool outputs, spill overflow to temp files, estimate tokens, fit messages before each LLM call.

| Function | Role |
|----------|------|
| `spill(text)` | Write full text to a temp file; track path in `SPILLS` |
| `cap(text)` | If longer than `CAP` (10k chars), keep head + point at spill path |
| `sweep()` | Delete spill files at end of turn |
| `locked(messages)` | Index after last compaction `<SUMMARY>` (do not trim above it) |
| `strip(messages)` | Shrink old tool results to `STUB` (300 chars) after compaction |
| `estimate(messages)` | Rough token estimate (`json length // 4`) |
| `fit(messages)` | Drop/trim tool bodies until under `CONTEXT_WINDOW * COMPACT_AT` |

**Connected to:** `tools` (cap), `subagent` / agent loop (`fit`), `compact` (`estimate`, `strip`).

---

### `compact.py` — summarize old turns when context is full

**Job:** when prompt tokens exceed the compact threshold, replace the middle of the transcript with a handoff summary + recent tail.

| Function | Role |
|----------|------|
| `needed(usage)` | True if `prompt_tokens > CONTEXT_WINDOW * COMPACT_AT` (ratio form) or past absolute threshold |
| `render(messages)` | Flatten transcript for the summarizer model |
| `summarize(messages)` | Extra LLM call (no tools) → handoff note |
| `safe_boundary(messages, start)` | Cut only where tool_call / tool_result pairs stay intact |
| `tail_start(messages, budget)` | How far back the “keep” tail can go |
| `compact(messages)` | `system + <summary> handoff + recent messages`; then `strip` |

**Connected to:** `config`, `history`, `llm.client`; triggered by `commands.compact` (`/compact`) and (planned) auto in the agent loop.

---

### `context.py` — per-turn environment injection

**Job:** append a small user/system reminder every turn so the model sees time, branch, todos, and files that changed since last turn.

| Function | Role |
|----------|------|
| `git(command)` | Run a git subcommand; return stdout |
| `file_hash(path)` | MD5 of file bytes (or None) |
| `git_state()` | map path → (status, hash) from `git status --porcelain` |
| `file_changes()` | Diff vs previous turn’s state |
| `changes_note()` | `<system-reminder>` listing changed files |
| `todos_note()` | Wrap `todos_prompt()` in `<todos>` |
| `reminder()` | Full message dict: `<env>` + todos + changes |

**Connected to:** `todos.todos_prompt`; consumed by the agent loop each turn.

---

### `session.py` — persist chats as JSONL

**Job:** one JSONL file per chat under `~/.agents/sessions/<project>/`.

| Function | Role |
|----------|------|
| `path_for(session_id)` | Path to that session’s `.jsonl` |
| `save(messages)` | Append only messages newer than `WRITTEN` |
| `rewind_to(count)` | Log a rewind marker; keep older bytes on disk |
| `compacted(messages)` | Log a compaction rewrite marker |
| `load(session_id)` | Replay log (messages + rewinds + compacteds) |
| `open_session(session_id)` | Switch `CURRENT` to a past chat |
| `title(messages)` | First user message, truncated |
| `all_sessions()` | Newest-first list of `{id, title}` |

**Connected to:** `commands` (`/rewind`, `/sessions`, `/compact`).

---

### `commands.py` — slash commands in the REPL

**Job:** handle local commands without calling the coding model.

| Function | Role |
|----------|------|
| `preview(message)` | One-line preview for rewind picker |
| `redraw(messages, label)` | Clear + banner + replay after history surgery |
| `rewind(messages)` | Pick a message index; truncate; `session.rewind_to` |
| `sessions(messages)` | Pick a past chat; `session.open_session` |
| `compact(messages)` | Run `compact.compact`; persist; show UI panel |
| `handle(command, messages)` | Dispatch `/compact`, `/rewind`, `/sessions`, or print help |

**Connected to:** `compact`, `session`, `sandbox`, `ui`.

---

### `skills.py` — loadable instruction packs

**Job:** discover `SKILL.md` files and expose them to the model as the `read_skill` tool.

| Symbol / function | Role |
|-------------------|------|
| `SKILL_DIRS` | `./.agents/skills` and `~/.agents/skills` |
| `find_skills()` | Parse YAML frontmatter → `{name: {description, path}}` |
| `SKILLS` | Cached map at import time |
| `skills_prompt()` | Bullet list for system prompt injection |
| `read_skill(name)` | Return full `SKILL.md` text (or error string) |

**Connected to:** registered in `tools.TOOLS` / `TOOL_SCHEMAS`.

---

### `todos.py` — in-session plan checklist

**Job:** let the model maintain a multi-step plan the UI can render and `context` can re-inject.

| Symbol / function | Role |
|-------------------|------|
| `MARKS` | `pending → [ ]`, `in_progress → [~]`, `done → [x]` |
| `TODOS` | Module-level list (mutated in place) |
| `write_todos(todos)` | Replace list; enforce at most one `in_progress` |
| `todos_prompt()` | Text checklist for prompts / tool results |
| `active_form()` | Present-continuous label for the spinner |
| `TODO_SCHEMA` | Tool schema for the model |

**Connected to:** `tools`, `context.todos_note`, `ui.todos` / `ui.working`.

---

### `subagent.py` — nested exploration agent

**Job:** implement the `task` tool — spawn a **read-only** child loop with its own messages/window, return a short report to the parent.

| Symbol / function | Role |
|-------------------|------|
| `MAX_TURNS` | Cap child loop (e.g. 12) |
| `WITHHELD` | Tool names the child must not get (`task`, `write_todos`, …) |
| `SYSTEM_PROMPT` | Child instructions: search, don’t edit, short report |
| `toolset()` | `TOOL_SCHEMAS` minus `WITHHELD` |
| `task(description)` | Child loop: `fit` → `call_llm` → `execute` → until done or max turns |
| `TASK_SCHEMA` | Schema registered on the parent tool list |

**Connected to:** `llm`, `tools.execute`, `history.fit`, `ui`; imported by `tools`.

**Design note:** withhold names must match real schema names (`str_replace`, `write_file`), or write tools still leak into the child.

---

### `__init__.py`

Empty package marker so `Axoscode` imports as a package.

---

## 4. Connection matrix (who imports whom)

| Module | Imports / uses |
|--------|----------------|
| `config` | (stdlib only) |
| `llm` | `config`, `tools` |
| `tools` | `history`, `sandbox`, `permissions`, `ui`, `skills`, `todos`, `subagent` |
| `permissions` | (stdlib / pathlib) |
| `sandbox` | (stdlib) |
| `history` | `config` |
| `compact` | `config`, `history`, `llm.client` |
| `context` | `todos` |
| `session` | (stdlib) |
| `prompt` | `prompt_toolkit` |
| `ui` | `prompt`, `todos`, `rich` |
| `commands` | `compact`, `session`, `sandbox`, `ui` |
| `skills` | `yaml` |
| `todos` | (none) |
| `subagent` | `history`, `llm`, `tools`, `ui` |
| `agent` | *(planned: all of the above)* |

Avoid eager circular imports: `subagent.toolset` and `tools.execute` already use **lazy imports** inside functions for that reason.

---

## 5. Data that lives outside the repo

| Path | Written by | Purpose |
|------|------------|---------|
| `~/.agents/.env` | you (optional) | Secrets / BASE_URL if using env-file loader |
| `~/.agents/history` | `prompt.py` | REPL input history |
| `~/.agents/sessions/<project>/*.jsonl` | `session.py` | Saved chats |
| `~/.agents/skills/*/SKILL.md` | you | Global skills |
| `./.agents/skills/*/SKILL.md` | you | Project-local skills |
| temp `axoscode--*.txt` | `history.spill` | Overflow tool output (deleted by `sweep`) |

---

## 6. How to run

```bash
cd Axos
uv sync

# LLM demo only (works once config + tools stubs/API key are set)
export AXOS_API_KEY=sk-...
uv run python -m Axoscode.llm

# Full CLI (once agent.main is implemented and entry point matches package name)
uv run axos
```

---

## 7. Honest status snapshot

| Area | Status |
|------|--------|
| Packaging / deps | `openai`, `rich`, `prompt-toolkit`, `pyyaml` declared |
| `config`, `llm`, `sandbox`, `permissions`, `session`, `commands`, `compact`, `context`, `prompt`, `skills` | Implemented on disk |
| `agent` | Stub `main()` only — **no loop yet** |
| `tools`, `history`, `ui`, `todos`, `subagent` | Designed in working buffers; **disk may still be empty/stub** — save them before expecting imports to work |
| Entry point name | `axos.agent:main` vs package folder `Axoscode` — still mismatched |

For the architectural overview and defect list, see [ARCHITECTURE.md](ARCHITECTURE.md).
