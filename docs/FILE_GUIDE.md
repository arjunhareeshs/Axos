# Axos — File Guide

What each file does, what it exposes, and what it depends on. For how the
pieces work together, see [ARCHITECTURE.md](ARCHITECTURE.md).

Package directory: `Axoscode/`. Console command: `axos`.

---

## 1. Project root

| File | Role |
|---|---|
| `pyproject.toml` | Package `axos`. Dependencies: `litellm`, `prompt-toolkit`, `pypdf`, `python-docx`, `pdfplumber`, `pillow`, `playwright`, `pyyaml`, `rich`. Script: `axos = "Axoscode.agent:main"`. |
| `uv.lock` | Locked dependencies for `uv sync`. |
| `.python-version` | Python 3.10. |
| `README.md` | Title only. |
| `docs/` | This guide and the architecture document. |

## 2. Running

```bash
uv sync
uv run playwright install chromium   # once, for the browser tools
uv run axos
```

Settings are `KEY=value` lines read from the `.env` in the Axos checkout, then
from `~/.agents/.env`; real environment variables override both. The checkout's
`.env` is git-ignored. Each setting has an `AXOS_`-prefixed alternative.

| Setting | Default | Meaning |
|---|---|---|
| `PROVIDER` | unset | LiteLLM provider, always prefixed to `MODEL`. When unset, `MODEL` carries it (`groq/…`); a bare name means `openai`. |
| `MODEL` | none (required) | Model id, bare or `provider/model`. Axos raises a clear error if it is not set. |
| `API_KEY` | unset | When unset, LiteLLM uses the provider's own environment key. |
| `BASE_URL` | unset | Custom endpoint. |
| `API_FORMAT` | `chat` | `chat` or `responses`. |
| `PROMPT_CACHE` | `1` | `0` turns cache markers off. |
| `CONTEXT_WINDOW` | `128000` | Window size in tokens. |
| `MAX_TOKENS` | `8192` | Cap on each model reply. |
| `VISION_MODEL` | unset | Model that describes images in documents; falls back to `MODEL`. Must support images and the chat API. |
| `MAX_DOC_IMAGES` | `10` | Images described per document (one model call each). |
| `MAX_RETRIES` | `5` | Retries for a transient model-API failure. |
| `MAX_RETRY_WAIT` | `60` | Longest single wait in seconds; a rate limit that resets later stops with a message. |
| `MAX_TURNS` | `100` | Model calls per user message. |
| `CHILD_MAX_TURNS` | `30` | Model calls per subagent. |
| `TOOL_DELAY_MS` | `500` | Pause after every tool call, so steps can be followed on screen. `0` turns it off. |
| `BROWSER_HEADLESS` | `auto` | `auto` shows a window when a display exists; `true` / `false` force it. |
| `BROWSER_TIMEOUT_MS` | `15000` | Timeout for one browser action. |
| `BROWSER_NAV_TIMEOUT_MS` | `30000` | Timeout for one page load. |
| `SYSTEM_PROMPT_FILE` | `Axoscode/prompts/system.md` | File holding the top-level instructions. |

---

## 3. Modules

### `prompts/system.md`

The top-level instructions sent as the system message: working style, code
conventions, tool use, planning, when to spawn subagents, verification, safety
and how to report. Edit it, or point `SYSTEM_PROMPT_FILE` at your own. Subagents
use their own prompt (`CHILD_PROMPT` in `subagent.py`).

### `config.py`

Loads the settings above. Also defines:

| Name | Value | Meaning |
|---|---|---|
| `COMPACT_AT` | `0.85` | Share of the window that triggers compaction. |
| `COMPACT_TO` | `0.35` | Share of the window kept as the recent tail. |
| `MAX_SPAWN` | `3` | Subagents per `spawn_agents` call. |
| `STUCK_LOOP_THRESHOLD` | `3` | Identical consecutive tool-call rounds before the turn ends. |
| `TOOL_CONCURRENCY_SAFE` | dict | Which tools may run in parallel (`read_file`, `read_skill`). |

Imports: standard library only.

### `agent.py` — main loop and entry point

| Name | Role |
|---|---|
| `_load_system_prompt()` | Reads `config.SYSTEM_PROMPT_FILE` once at startup, plus the skills list. Exits with a clear message if the file is unreadable. |
| `SYSTEM_PROMPT` | The loaded instructions; constant for the process. |
| `_build_messages(messages)` | System prompt + history + the per-turn reminder, last. |
| `_model_turn(messages)` | One streamed model call. Appends the assistant message, prints text and usage, returns the tool calls. |
| `_run_batches(tool_calls)` | Runs parallel-safe batches with `asyncio.gather`, others one at a time. |
| `_respond(messages)` | Loops model turn → tools until no tool calls, a stuck loop, a failed model call, or `MAX_TURNS`. Saves, compacts, deletes spill files. |
| `_compact(messages)` | Runs compaction; on failure keeps the transcript. |
| `run()` | The REPL: input, slash commands, `_respond`. |
| `main()` | Console entry point; runs `run()`. |

Imports: `commands`, `compact`, `config`, `context`, `history`, `sandbox`,
`session`, `subagent`, `llm`, `tools`, `ui`.

### `llm.py` — LiteLLM gateway

| Name | Role |
|---|---|
| `get_model()` | The LiteLLM model id. |
| `call_llm(messages, tools)` | Blocking call. Returns `(message, usage)`. |
| `call_llm_stream(messages, tools)` | Async generator of `content_block_delta`, `tool_call` and `usage` events. |
| `complete_text(messages)` | Blocking call with no tools; returns text. |
| `_with_cache_markers(messages)` | Adds `cache_control` breakpoints for Anthropic-style providers, on a copy. |
| `_clean_tools`, `_responses_tools` | Tool schemas for each API format, without Axos-only keys. |
| `_responses_input(messages)` | Chat history → Responses input items. |
| `_usage(u)` | One usage shape for both formats: prompt, completion, reasoning, cached, total. |
| `_Message`, `_make_call` | Chat-shaped message and tool-call objects for the Responses path. |

Running `python -m Axoscode.llm` sends one prompt and prints the reply.

`call_llm`, `call_llm_stream`, `complete_text` and `describe_image` all run
under `failures`' retry policy.

Imports: `litellm`, `config`, `failures`, `tools`.

### `failures.py` — fault tolerance

Classifies every model-API failure and either retries it or stops with a clear
message. Callers only ever see `LLMError(kind, message)`.

| Name | Role |
|---|---|
| `classify(failure)` | `(kind, limit)`: `quota`, `rate_limit` (with `RPM`/`RPD`/`TPM`/`TPD` when the provider names it), `auth`, `context`, `request`, `transient`, `unknown`. |
| `retry_after(failure)` | Seconds the provider asks you to wait (headers, or "try again in 6m0s"). |
| `decide(failure, attempt, model)` | The delay before the next try, or raises `LLMError`. Shows each retry with `ui.alert`. |
| `with_retries`, `with_retries_async` | Run a blocking or async call under that policy. |

Retried with backoff: rate limits that reset within `MAX_RETRY_WAIT`, timeouts,
connection errors and 5xx. Stopped at once: out of credits, bad key, a daily
limit, a context overflow, a rejected request. A stream that fails before its
first event is retried; one that fails after text was delivered is not, so
nothing is repeated.

Imports: `config`, `litellm`; `ui` lazily.

### `tools.py` — tool registry and execution

| Name | Role |
|---|---|
| `bash(command)` | Runs through `sandbox.run`; output capped. |
| `read_file`, `write_file`, `str_replace` | File tools. Paths resolve through `workspace.resolve`. `read_file` also parses PDF, `.docx` and image files (via `documents`). `str_replace` requires a unique match unless `allow_multi_edit`. |
| `execute(tool_call)` | Parses arguments, checks permissions, asks for approval if needed, runs the handler. Returns `(args, result)`; never raises. |
| `execute_async(tool_call)` | `execute` in a worker thread. |
| `partition_tool_calls(calls, safety_map)` | Groups consecutive parallel-safe calls; every other call is its own batch. |
| `TOOL_SCHEMAS`, `TOOLS` | Schemas sent to the model, and the name → handler map. |
| `APPROVAL_LOCK` | One approval prompt at a time. |

Tools registered: `bash`, `read_file`, `read_skill`, `write_file`,
`str_replace`, `write_todos`, `spawn_agents`, and the 34 `browser_*` tools from
`browser.py`. `tools_prompt()` renders the whole registry as the "Tool
reference" section of the system prompt.

Imports: `browser`, `documents`, `history`, `sandbox`, `workspace`, `permissions`, `subagent`,
`skills`, `todos`; `ui` lazily.

### `subagent.py` — dynamic subagents

| Name | Role |
|---|---|
| `AgentSpec` | A requested child: `name`, `role`, `task`, `write`, `browser`, `tools`, `depends_on`. |
| `AgentResult` | A child's result: `name`, `status`, `summary`, `findings`, `changes`, `tests`, `patch`. |
| `READ_TOOLS`, `WRITE_TOOLS` | What a child can be granted. |
| `CHILD_PROMPT` | Constant system prompt shared by all children. |
| `set_parent_context(messages)` | Called by the main loop before tools run. |
| `fork_context()` | Filtered, trimmed copy of the parent history. |
| `allowed_tools(spec)` | The child's tool schemas. |
| `run_child(spec, context, upstream)` | The child's loop, up to `CHILD_MAX_TURNS`. |
| `parse_result(name, text)` | Reads the child's JSON result, falling back to raw text. |
| `schedule(specs)` | Orders children into waves by `depends_on`. |
| `spawn_agents(agents)` | The tool: validate, fork, run waves in threads, return the JSON report. |
| `SPAWN_SCHEMA` | The tool definition. |

Imports: `config`, `history`, `session`, `workspace`; `compact`, `llm`,
`tools`, `ui` lazily, to avoid import cycles.

### `workspace.py` — working directory and worktrees

| Name | Role |
|---|---|
| `PROJECT` | The directory Axos was started in. |
| `current()` | The calling agent's working directory. |
| `use(root)` | Context manager that sets it for the current thread. |
| `resolve(path)` | Maps a path into the current working directory. |
| `snapshot()` | Commit id of the project now, uncommitted tracked edits included; `None` without a repository or commits. |
| `create(base)` | New worktree in a temp directory. |
| `collect(tree, base)` | `(changed files, patch file path)` for a worktree. |
| `remove(tree)` | Deletes the worktree. |

Imports: standard library only.

### `browser.py` — browser control (Playwright)

One file: the runtime, the sessions and all 34 `browser_*` tools.

| Name | Role |
|---|---|
| `Runtime` / `RUNTIME` | A background thread with its own event loop that owns Playwright. `run(coroutine)` executes on it from any thread. |
| `Session` | One agent's browser: process, context, tabs, element refs, console and network logs, pending events, dialog policy, mocks. `page()` repairs a dead browser or tab before returning the active tab; `status()` builds the footer and snapshots cookies/storage. |
| `OWNER`, `owned_by(name)` | Which session a tool call belongs to: `main`, or a subagent's name. `owned_by` closes the session on exit. |
| `tool(...)` | Decorator that registers an async implementation in `SCHEMAS` and `TOOLS` as a blocking call that never raises. |
| `explain(failure)` | Shortens a Playwright error and adds what to do next. |
| `settle(page)`, `quiet(page)` | Wait for navigation and for network idle after an action, without failing. |
| `shutdown()` | Closes every session; called on exit. |

Tools, by area:

| Area | Tools |
|---|---|
| Lifecycle | `browser_create_session`, `browser_close_session` |
| Navigation | `browser_navigate`, `browser_history` |
| Inspection | `browser_get_page_info`, `browser_get_accessibility`, `browser_get_dom`, `browser_evaluate` |
| Interaction | `browser_click`, `browser_fill`, `browser_select`, `browser_hover`, `browser_press_key`, `browser_scroll`, `browser_drag`, `browser_mouse`, `browser_upload`, `browser_handle_dialog` |
| Waiting | `browser_wait_for` |
| Screenshots, size | `browser_screenshot`, `browser_resize` |
| Tabs | `browser_list_tabs`, `browser_new_tab`, `browser_switch_tab`, `browser_close_tab` |
| Debugging | `browser_get_console`, `browser_get_network`, `browser_mock_network`, `browser_start_trace`, `browser_stop_trace` |
| Authentication | `browser_save_state`, `browser_load_state`, `browser_cookies` |
| Testing | `browser_assert` |

Elements are addressed by a ref from `browser_get_accessibility` (which tags
visible elements in every frame) or by a Playwright selector. Every result ends
with a `[page]` line and, when something happened, an `[events]` line.

Imports: `playwright`, `config`, `history`, `workspace`.

### `documents.py` — document parser

`read(path)` turns a file into Markdown text. `.pdf`, `.docx` and image files
(`.png .jpg .jpeg .gif .webp .bmp`) are parsed; anything else is read as UTF-8.

| Name | Role |
|---|---|
| `pdf_markdown(path)` | Per page, top to bottom: text, and each table as a Markdown table (`pdfplumber`), then the page's images. Marks pages `--- page N ---`. |
| `docx_markdown(path)` | Walks the body in order: headings (`#`), bullets (`-`), tables, and images where they occur. |
| `image_markdown(path)` | A standalone image: its description. |
| `markdown_table(rows)` | Cells → Markdown table. |
| `Images` | Describes a document's images within `MAX_DOC_IMAGES`; skips images under 48 px. |

Each image becomes a note with its size and a temp-file path (deleted when the
turn ends), followed by the vision model's description via
`llm.describe_image`. If no description is possible (no vision model, no
credit, an error), the note says why and the rest of the document is still
returned. Scanned PDFs work this way: each page image is transcribed.

Errors: password-protected PDF, a document with nothing readable, legacy `.doc`.

Imports: `config`, `history`; `llm`, `pdfplumber`, `pypdf`, `docx` and `PIL`
only when needed.

### `permissions.py`

| Name | Role |
|---|---|
| `BASH_RULES` | Pattern → `allow` / `ask` / `deny`. Later rules override earlier ones. |
| `split_command(command)` | Splits on `| & ;` outside quotes. |
| `decide(command)` | Strictest verdict across the parts. |
| `inside_workspace(path)` | Whether a path is inside the caller's working directory. |
| `check(name, args)` | `(action, reason)` for a tool call. |

Imports: `workspace`.

### `sandbox.py`

| Name | Role |
|---|---|
| `writable(cwd)` | The project, or for a worktree: the worktree and the repository's `.git`. |
| `profile(cwd)` | macOS seatbelt profile. |
| `wrap(command, cwd)` | The sandboxed argv, or `None` when no sandbox is available. |
| `name()` | `seatbelt`, `bubblewrap` or `none`. |
| `run(command, timeout=60)` | Runs in the caller's workspace. |

Imports: `workspace`.

### `history.py`

| Name | Role |
|---|---|
| `cap(text)` | Cuts output over `CAP` (10,000 characters) and spills the full text to a temp file. |
| `spill(text)`, `sweep()` | Write a spill file; delete all spill files. |
| `estimate(messages)` | Token estimate: JSON length ÷ 4. |
| `fit(messages)` | Drops old tool outputs until under the compaction threshold. |
| `strip(messages)` | Cuts tool outputs to `STUB` (300 characters) after compaction. |
| `locked(messages)` | Index after the last compaction summary. |

Imports: `config`.

### `compact.py`

| Name | Role |
|---|---|
| `compact(messages)` | Handoff summary + recent tail. Returns the same list when there is nothing to compact. |
| `summarize(messages)` | One model call through `llm.complete_text`. |
| `render(messages)` | Flattens a transcript for the summariser. |
| `tail_start`, `safe_boundary` | Where the kept tail begins, without splitting a tool call from its result. |
| `needed(usage)` | Whether reported prompt tokens passed the threshold. |

Imports: `config`, `history`, `llm`.

### `context.py`

| Name | Role |
|---|---|
| `reminder()` | The per-turn message: `<env>` (time, branch), todos, changed files. |
| `file_changes()`, `git_state()` | Files whose git status or content changed since the last call. |

Imports: `todos`.

### `session.py`

| Name | Role |
|---|---|
| `save(messages)` | Appends messages not yet on disk. |
| `rewind_to(count)`, `compacted(messages)` | Append a rewind marker or a compaction rewrite. |
| `log_agent(event)` | Appends a subagent spawn or finish record. |
| `load(session_id)` | Replays a file into a message list. |
| `open_session(session_id)` | Switches the current session. |
| `all_sessions()` | `{id, title}` list, newest first. |

Imports: standard library only.

### `commands.py`

`handle(command, messages)` dispatches `/compact`, `/rewind` and `/sessions`;
anything else prints the command list.

Imports: `compact`, `sandbox`, `session`, `ui`.

### `skills.py`

Finds `SKILL.md` files in `./.agents/skills` and `~/.agents/skills` at import
time. `read_skill(name)` returns one; `skills_prompt()` is the system-prompt
section listing them, used by the main agent and by subagents.

Imports: `yaml`.

### `todos.py`

`write_todos(todos)` replaces the checklist (at most one item `in_progress`).
`todos_prompt()` renders it; `active_form()` gives the current item's label.

Imports: none.

### `ui.py`

A single `ui = UI()` object built on Rich.

| Method | Role |
|---|---|
| `banner`, `clear`, `resumed`, `replay` | Startup line and redrawing a transcript. |
| `ask()` | Reads a user message; `None` on Ctrl-D or Ctrl-C. |
| `approve(reason)` | Yes/no prompt for a tool call. |
| `pick(title, rows)` | Numbered chooser. |
| `agent(text)`, `user(text)`, `note(text)` | Message output. |
| `tool(name, args, result, owner=None)` | Tool-call panel; `owner` is the subagent's name. |
| `subagent(name, description)` | Panel shown when a child is spawned. |
| `alert(text)` | Highlighted warning: a retry, a limit, an outage. |
| `usage(stats)`, `summary()` | Per-call token line and session totals. |
| `compacted(before, messages)`, `todos(todos)` | Compaction and checklist panels. |
| `working(label)` | Spinner context manager. |

Imports: `prompt`, `todos`, `rich`.

### `prompt.py`

`read(prompt)` reads one line with prompt_toolkit, with history in
`~/.agents/history`. Alt-Enter inserts a newline.

---

## 4. Files written outside the repository

| Path | Written by | Purpose |
|---|---|---|
| `~/.agents/.env` | you | Settings, as an alternative to the checkout's `.env`. |
| `~/.agents/history` | `prompt.py` | Input history. |
| `~/.agents/sessions/<project>/*.jsonl` | `session.py` | Transcripts and subagent records. |
| `~/.agents/skills/*/SKILL.md`, `./.agents/skills/*/SKILL.md` | you | Skills. |
| temp `axoscode--*.txt` | `history.spill` | Full text of capped tool output. |
| temp `axos-worktree-*/` | `workspace.create` | A writing subagent's worktree; removed when it ends. |
| temp `axos-*.patch` | `workspace.collect` | A writing subagent's changes. |
| temp `axos-browser-<pid>/` | `browser.py` | Screenshots, traces and downloads. Not deleted on exit. |
| `~/.agents/browser/*.json` | `browser_save_state` | Saved logins (cookies and storage). Readable only by you. |
