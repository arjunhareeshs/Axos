# Axos — Architecture

> **Status: prototype (2026-11-14).** All modules are implemented. The code has
> been exercised with mocked model calls only; it has not yet been run against a
> live provider. There is no test suite in the repository. Known gaps are listed
> in [section 8](#8-known-gaps).

For a per-file reference, see [FILE_GUIDE.md](FILE_GUIDE.md).

---

## 1. What Axos is

Axos is a terminal coding agent: a REPL where a model reads and edits a
repository through a small set of tools. It is a `uv`-managed Python package
(`Axoscode/`, console command `axos`) of about 2,300 lines.

Design choices that shape everything else:

- **Provider-agnostic.** Every model call goes through LiteLLM, in either the
  chat-completions or the Responses API format.
- **Append-only history.** The conversation is only ever appended to, so the
  provider can cache the unchanged prefix.
- **Dynamic subagents.** There are no predefined agent types. The main agent
  decides at run time whether to spawn children, how many, and what each does.
- **Two safety layers.** A permission layer decides what needs your approval;
  an OS sandbox decides what is possible at all.

## 2. Module map

```
Axoscode/
├── agent.py        main loop and CLI entry point
├── llm.py          LiteLLM gateway: chat + Responses formats, prompt caching
├── prompts/
│   └── system.md   the model's top-level instructions
├── config.py       settings from .env, ~/.agents/.env and the environment
├── tools.py        tool registry, execute(), concurrency batching
├── subagent.py     spawn_agents: dynamic, parallel, context-forked children
├── browser.py      Playwright browser control: sessions and all browser_* tools
├── documents.py    PDF / Word / image → Markdown for read_file
├── failures.py     classify API failures; retry, or stop with a clear message
├── workspace.py    working directory per agent; git worktrees and patches
├── permissions.py  allow / ask / deny rules per tool call
├── sandbox.py      OS isolation for shell commands (bubblewrap, seatbelt)
├── history.py      output capping, token estimate, window fitting
├── compact.py      summarise old history when the window fills
├── context.py      per-turn reminder: time, branch, todos, changed files
├── session.py      JSONL transcripts and subagent lifecycle records
├── commands.py     /compact, /rewind, /sessions
├── skills.py       SKILL.md discovery and the read_skill tool
├── todos.py        the write_todos checklist
├── ui.py           Rich terminal output and approval prompts
└── prompt.py       prompt_toolkit line editor (user input, not the system prompt)
```

## 3. The main loop

`agent.run()` is the REPL. For each user message, `agent._respond()` loops until
the model answers without calling tools:

```
user input
   │  "/..." → commands.handle (no model call)
   ▼
messages.append(user)                      history is append-only
   ▼
┌─► _model_turn
│     [system prompt] + history + [reminder]     reminder is sent, never stored
│     history.fit → llm.call_llm_stream
│     assistant message appended
│        │ no tool calls → done, back to the prompt
│        ▼
│   stuck check: same calls 3 times in a row → end the turn
│        ▼
│   _run_batches
│     consecutive read-only calls run in parallel (asyncio.gather)
│     every other call runs alone, in order
│     each call: tools.execute → permissions.check → ui.approve if "ask"
│        ▼
│   tool results appended → session.save
│   compact if the estimate passes CONTEXT_WINDOW × COMPACT_AT
└────────┘   (at most MAX_TURNS (default 100) model calls per user message)
```

`tools.execute` never raises. A bad tool name, invalid JSON, wrong arguments or
a failing handler all come back as text the model can read and retry.

Transient model-API failures are retried with backoff (`failures.py`) and each
wait is shown. A failure that cannot be retried (out of credits, bad key, a
daily limit) ends the turn with a specific message and leaves the history
intact; a failed compaction keeps the transcript as it
was. Neither ends the session.

## 4. The LLM gateway

`llm.py` is the only module that talks to a model.

| Setting | Effect |
|---|---|
| `PROVIDER`, `MODEL` | The LiteLLM model id. With `PROVIDER` set, it is always prefixed to `MODEL` (unless already there). With it unset, `MODEL` carries the provider (`groq/…`), and a bare name means `openai`. |
| `API_KEY`, `BASE_URL` | Optional. When unset, LiteLLM uses the provider's own environment key and endpoint. |
| `API_FORMAT` | `chat` (default) or `responses`. |
| `PROMPT_CACHE` | `1` (default) or `0`. |

Callers always work in chat-completions shapes. In `responses` mode the gateway
converts the history and tool schemas on the way out and maps output items back
to the same tool-call objects, so `agent.py` and `subagent.py` do not change with
the format.

Three entry points:

- `call_llm_stream(messages, tools)` — async, used by the main loop. Yields
  text deltas, completed tool calls and a usage event.
- `call_llm(messages, tools)` — blocking, used by subagents. Returns
  `(message, usage)`.
- `complete_text(messages)` — blocking, no tools, used by compaction.

### Prompt caching

Providers cache by exact prefix, so the request is laid out stable-first:

1. The system prompt — read from `prompts/system.md` (or `SYSTEM_PROMPT_FILE`)
   once at startup, with the list of available skills appended. Constant for
   the life of the process.
2. Tool schemas — fixed order.
3. History — append-only.
4. The per-turn reminder — volatile, always last, never stored.

OpenAI and Gemini cache a stable prefix automatically. For `anthropic`,
`bedrock` and `vertex_ai` model ids, `_with_cache_markers` adds `cache_control`
breakpoints to the system message and the last stable message. In `responses`
mode a `prompt_cache_key` is sent.

## 5. Subagents

The main agent has one tool for delegation, `spawn_agents`. It is told to do the
work itself by default and to spawn only when a task splits into independent
parts.

```
spawn_agents([{name, role, task, write?, tools?, depends_on?}, ...])   max 3
   │
   ├─ validate: count ≤ MAX_SPAWN, unique names, grantable tools, no cycles
   ├─ fork_context(): filtered copy of the parent's history
   ├─ schedule(): order into waves by depends_on
   │
   ├─ wave 1 ── child A ─┐
   │            child B ─┤  one thread each, in parallel
   ├─ wave 2 ── child C ─┘  (receives the summaries of what it depends on)
   │
   └─ JSON report: one result per child → returned as the tool result
```

**Lifecycle.** A child exists for one `spawn_agents` call. It ends when it
reports, when it reaches `CHILD_MAX_TURNS` (default 30), or when it raises. A failure in
one child is reported as `status: failed` and does not stop its siblings. The
main agent cannot stop a child mid-run. Spawn calls run one at a time, so at
most `MAX_SPAWN` children are alive at once. Children cannot spawn.

**Context.** Each child's request is: a constant child system prompt, the forked
parent history, then a final message with its name, role, task and working
directory. The fork is a copy: tool outputs are cut to stubs, it is trimmed to
the most recent `COMPACT_TO` share of the window, and nothing a child does
reaches the parent's history. Siblings share an identical prefix up to that
final message.

**Capabilities.**

| Child | Tools | Works in |
|---|---|---|
| default | `bash`, `read_file`, `read_skill` | the project |
| `write: true` | the above plus `write_file`, `str_replace` | its own git worktree |
| `browser: true` | adds all `browser_*` tools | its own browser session |

`tools` can narrow the set further. `write_todos` and `spawn_agents` are never
granted. A call to a tool outside the child's set is refused before it reaches
`execute`.

**Result contract.** Each child returns:

```json
{
  "name": "…", "status": "completed | partial | failed",
  "summary": "…", "findings": ["…"], "changes": ["…"], "tests": ["…"],
  "patch": "/tmp/axos-….patch"
}
```

The child writes the result as JSON in its last message; if that is not valid
JSON, the raw text becomes the summary. For a writing child, `changes` and
`patch` are replaced by what the harness measured with `git diff`.

### Workspace isolation

`workspace.py` holds the working directory of the calling agent in a context
variable. File tools resolve paths through it, shell commands run in it, and the
permission check uses it as "inside the project".

For a writing child:

1. `snapshot()` takes a commit id for the project as it is now, uncommitted
   edits to tracked files included (`git stash create`).
2. `create()` checks that commit out into a worktree in a temp directory.
3. The child runs with that worktree as its workspace. An absolute path into
   the main project is re-rooted into the worktree. Under bubblewrap, the
   child's shell can write only to the worktree and the repository's `.git`.
4. `collect()` stages everything and writes the diff to a patch file.
5. `remove()` deletes the worktree, also on failure.

Nothing reaches the main tree until the main agent runs `git apply <patch>`,
which goes through the normal approval prompt.

### Browser

`browser.py` gives the main agent and, when spawned with `browser: true`, a
subagent a real browser through 34 `browser_*` tools.

- **One thread owns Playwright.** Tool calls come from the main loop's worker
  threads and from parallel subagents; each is handed to a background event
  loop, so a session survives across turns and threads.
- **One session per agent.** The main agent's session is `main`; a subagent's
  is keyed by its name, has its own cookies and tabs, and is closed when the
  subagent ends.
- **Refs.** `browser_get_accessibility` tags the visible, meaningful elements
  of every frame and returns them with refs (`e1`, `e2`, …). Tools accept a ref
  or a Playwright selector. A ref from an old snapshot is rejected with an
  instruction to take a new one.
- **Every result reports state.** It ends with the page title, URL and tab,
  plus anything that happened since the last call: dialogs and how they were
  answered, downloads, new tabs, console errors, failed requests.
- **Waiting.** Actions wait for the element to exist and be actionable, then
  for navigation and network idle. `browser_wait_for` waits on a condition;
  `browser_scroll` with `load_all` scrolls until a lazy page stops growing.
- **Failures.** No tool raises. Errors are shortened and say what to do next.
  A browser or tab that died is restarted on the next call with cookies and
  storage restored from the last snapshot and the last page reopened.

## 6. Safety

- **Permissions** (`permissions.py`). Shell commands are split on `| & ;`
  outside quotes and each part is matched against `BASH_RULES`; the strictest
  verdict wins. Read-only commands are allowed, a fixed list (`rm`, `sudo`,
  `curl`, `git push`, …) is denied, everything else asks. `write_file` and
  `str_replace` ask when the path is outside the caller's workspace.
- **Sandbox** (`sandbox.py`). Shell commands run under bubblewrap on Linux or
  seatbelt on macOS: no network, the filesystem read-only except the caller's
  workspace. With neither available, commands run unsandboxed and the banner
  says `sandbox: none`.
- **Browser.** Opening a session asks for approval, because it gives the agent
  network access that shell commands do not have. Uploading a local file to a
  page asks too. Cookie values and password fields are never returned.
- **Approval prompts** are serialised by a lock, so parallel children ask one
  at a time.

## 7. Context management and persistence

- **Capping.** A tool output over 10,000 characters is cut, and the full text
  is written to a temp file the model can page through. The files are deleted
  when the turn ends.
- **Fitting.** Before each model call, `history.fit` drops old tool outputs
  until the estimate (JSON length ÷ 4) is under the compaction threshold.
- **Compaction.** Past `CONTEXT_WINDOW × COMPACT_AT` (85%), `compact.compact`
  replaces older history with a model-written handoff summary and keeps a
  recent tail of about `COMPACT_TO` (35%), cut only where no tool call is
  separated from its result. `/compact` runs it on demand.
- **Sessions.** `session.py` appends to one JSONL file per chat under
  `~/.agents/sessions/<project>/`. Besides messages, the file holds rewind
  markers, compaction rewrites and subagent spawn/finish records. `load`
  replays the file; subagent records are skipped.

## 8. Known gaps

| Area | Gap |
|---|---|
| Verification | The real `axos` command was run in a pseudo-terminal: startup, slash commands, a failed model call, exit. The tool loop, approvals and a visible browser were run through the real REPL with a scripted stand-in for the model. No live model has driven it yet. No automated tests in the repository. |
| Main loop | Replies are printed when complete, not as they stream. |
| Caching | `history.fit` edits old tool messages in place, which changes the cached prefix on the turns where it trims. |
| Compaction | The summary call is blocking inside the async loop. |
| Subagents | Untracked files are not copied into a worktree. Overlapping patches are not merged automatically. A dependent child receives summaries, not file changes. Children cannot be stopped mid-run. |
| Sandbox | Without bubblewrap or seatbelt, a writing child's shell can reach the main tree. The macOS profile is untested. |
| Documents | PDF images are listed per page, not at their position in the text. Tables inside Word table cells and Word images in headers or text boxes are skipped. Image descriptions need a vision-capable chat model and credit; not yet verified against a live model. |
| Browser | Not yet driven by a live model. Once a session is approved, navigation is unrestricted: there is no allow-list of sites. Refs do not reach inside closed shadow roots. A visible window was only smoke-tested; the full test ran headless. Screenshots, traces and downloads are left in the temp directory after exit. |
| UI | Terminal input (the prompt, approvals, pickers) runs in worker threads because the main loop is async. Tested in a pseudo-terminal with one approval at a time; several subagents asking at once is untested. |
