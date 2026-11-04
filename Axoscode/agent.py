"""Axos core agent loop.

Streaming, safety-partitioned tool batching, stuck-loop detection, and
async parallel execution for independent tool calls.
"""

import asyncio

from . import (
    config,
    context,
    compact,
    history,
    session,
    ui,
    commands,
    sandbox,
)
from .llm import call_llm_stream
from .tools import TOOL_SCHEMAS, execute_async, partition_tool_calls


def _system_prompt() -> str:
    return (
        "You are Axos, a terminal coding agent. "
        "Use read-only tools in parallel batches; mutating tools "
        "(write_file, str_replace, bash) run one at a time. "
        "When finished, answer without calling more tools."
    )


def _build_messages(messages: list, user_input: str) -> list:
    msgs = [{"role": "system", "content": _system_prompt()}]
    reminder = context.reminder()
    if reminder["content"].strip():
        msgs.append(reminder)
    msgs.extend(messages)
    msgs.append({"role": "user", "content": user_input})
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


async def main() -> None:
    ui.banner(sandbox.name())

    messages: list = []
    turn = 0
    stuck_count = 0
    last_signatures: list = []

    while turn < config.MAX_TURNS:
        turn += 1

        try:
            user_input = ui.ask()
        except (EOFError, KeyboardInterrupt):
            ui.note("")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            messages = commands.handle(user_input, messages)
            continue

        msgs = _build_messages(messages, user_input)
        history.fit(msgs)

        tool_calls = []
        assistant_text = []

        async for event in call_llm_stream(msgs, tools=TOOL_SCHEMAS):
            etype = event["type"]

            if etype == "content_block_delta":
                text = event.get("text", "")
                if text:
                    assistant_text.append(text)
            elif etype == "tool_call":
                tool_calls.append(event["call"])

        full_text = "".join(assistant_text)

        assistant_msg = {"role": "assistant", "content": full_text or None}
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

        if not tool_calls:
            session.save(messages)
            continue

        new_signatures = [_tool_signature(tc) for tc in tool_calls]
        if _detect_stuck(last_signatures, new_signatures):
            stuck_count += 1
        else:
            stuck_count = 0
        last_signatures = new_signatures

        if stuck_count >= config.STUCK_LOOP_THRESHOLD:
            ui.note(f"Stuck loop detected ({stuck_count} repeats); ending turn.")
            session.save(messages)
            break

        executed = await _run_batches(tool_calls)

        for tool_call, (args, result) in executed:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )
            ui.tool(tool_call.function.name, args, result)

        session.save(messages)

        if history.estimate(messages) > config.CONTEXT_WINDOW * config.COMPACT_AT:
            before = len(messages)
            messages = compact.compact(messages)
            session.compacted(messages)
            ui.compacted(before, messages)

    ui.summary()


if __name__ == "__main__":
    asyncio.run(main())
