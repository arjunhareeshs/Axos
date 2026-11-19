"""Axos core agent loop.

Streaming, safety-partitioned tool batching, stuck-loop detection, and
async parallel execution for independent tool calls.
"""

import asyncio

import litellm

from . import browser, commands, compact, config, context, history, sandbox, session, subagent
from .failures import LLMError
from .llm import call_llm_stream
from .skills import skills_prompt
from .tools import TOOL_SCHEMAS, execute_async, partition_tool_calls, tools_prompt
from .ui import ui


def _load_system_prompt() -> str:
    """Read the instructions once at startup.

    Constant for the life of the process on purpose: a byte-identical prefix
    is what the provider caches.
    """
    try:
        text = config.SYSTEM_PROMPT_FILE.read_text().strip()
    except OSError as failure:
        raise SystemExit(
            f"Cannot read the system prompt {config.SYSTEM_PROMPT_FILE}: {failure}"
        )
    return text + tools_prompt() + skills_prompt()


SYSTEM_PROMPT = _load_system_prompt()


def _build_messages(messages: list) -> list:
    """History is append-only; the volatile reminder goes last, unstored."""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    reminder = context.reminder()
    if reminder["content"].strip():
        msgs.append(reminder)
    return msgs


def _tool_signature(tool_call) -> tuple:
    return (tool_call.function.name, tool_call.function.arguments)


async def _run_batches(tool_calls: list) -> list:
    """Partition by concurrency safety, run safe batches in parallel.

    Returns list of (tool_call, (args, result)) in original order.
    """
    batches = partition_tool_calls(tool_calls, config.TOOL_CONCURRENCY_SAFE)
    results = []

    for batch in batches:
        first_name = batch[0].function.name
        is_safe = config.TOOL_CONCURRENCY_SAFE.get(first_name, False)

        if len(batch) == 1 and not is_safe:
            result = await execute_async(batch[0])
            results.append((batch[0], result))
        else:
            batch_results = await asyncio.gather(
                *[execute_async(tc) for tc in batch]
            )
            results.extend(zip(batch, batch_results))

    return results


def _detect_stuck(last: list, current: list) -> bool:
    return last == current


async def _model_turn(messages: list) -> list:
    """One model call. Appends the assistant message; returns its tool calls."""
    msgs = _build_messages(messages)
    history.fit(msgs)

    text, tool_calls = [], []
    async for event in call_llm_stream(msgs, tools=TOOL_SCHEMAS):
        if event["type"] == "content_block_delta":
            text.append(event["text"])
        elif event["type"] == "tool_call":
            tool_calls.append(event["call"])
        elif event["type"] == "usage":
            ui.usage({k: v for k, v in event.items() if k != "type"})

    content = "".join(text)
    if content:
        ui.agent(content)

    assistant_msg = {"role": "assistant", "content": content or None}
    if tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    messages.append(assistant_msg)
    return tool_calls


def _compact(messages: list) -> list:
    """Summarise old history. A failure here keeps the transcript as it is."""
    try:
        compacted = compact.compact(messages)
    except Exception as failure:  # noqa: BLE001
        ui.note(f"Compaction failed ({type(failure).__name__}); transcript kept as is.")
        return messages
    if compacted is not messages:
        session.compacted(compacted)
        ui.compacted(len(messages), compacted)
    return compacted


async def _respond(messages: list) -> list:
    """Work on the latest user message until the model stops calling tools."""
    stuck_count = 0
    last_signatures: list = []

    for _ in range(config.MAX_TURNS):
        try:
            tool_calls = await _model_turn(messages)
        except Exception as failure:  # noqa: BLE001 - provider/network errors
            # Nothing was appended, so the history is intact: just retry.
            ui.alert(
                str(failure)
                if isinstance(failure, LLMError)
                else f"Model call failed ({type(failure).__name__}: {failure})."
            )
            ui.note("Your request is kept; send a message to continue.")
            break
        if not tool_calls:
            break

        signatures = [_tool_signature(tc) for tc in tool_calls]
        stuck_count = stuck_count + 1 if _detect_stuck(last_signatures, signatures) else 0
        last_signatures = signatures
        if stuck_count >= config.STUCK_LOOP_THRESHOLD:
            ui.note(f"Stuck loop detected ({stuck_count} repeats); ending turn.")
            # Every tool call needs a result, or the next request is rejected.
            for tool_call in tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "Not run: the same calls were repeated; turn ended.",
                    }
                )
            break

        subagent.set_parent_context(messages)
        for tool_call, (args, result) in await _run_batches(tool_calls):
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": result}
            )
            if tool_call.function.name != "spawn_agents":
                ui.tool(tool_call.function.name, args, result)

        session.save(messages)

        if history.estimate(messages) > config.CONTEXT_WINDOW * config.COMPACT_AT:
            messages = _compact(messages)
    else:
        ui.note(f"Stopped at the {config.MAX_TURNS}-call limit for one prompt.")

    session.save(messages)
    history.sweep()  # spilled tool output only lives for the turn
    return messages


async def run() -> None:
    ui.banner(sandbox.name())
    messages: list = []

    try:
        while True:
            # Terminal input runs its own event loop, so it gets its own thread.
            user_input = await asyncio.to_thread(ui.ask)
            if user_input is None:
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                messages = await asyncio.to_thread(commands.handle, user_input, messages)
                continue

            messages.append({"role": "user", "content": user_input})
            messages = await _respond(messages)
    finally:
        # Close LiteLLM's HTTP clients while the event loop is still running.
        await litellm.close_litellm_async_clients()
        await asyncio.to_thread(browser.shutdown)

    ui.summary()


def main() -> None:
    """Console-script entry point (`axos`)."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
