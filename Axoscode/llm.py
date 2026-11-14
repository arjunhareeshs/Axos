"""Global LLM gateway built on LiteLLM.

One provider-agnostic entry point for every model call in Axos. LiteLLM routes
to OpenAI, Anthropic, Gemini, Ollama, etc. from the model id, and can speak
either the chat-completions or the Responses API (config.API_FORMAT).

Public surface (unchanged for callers):
    call_llm(messages, tools=None)         -> (message, usage)
    call_llm_stream(messages, tools=None)  -> async events
    complete_text(messages)                -> str   (no tools, for compaction)
"""

import json
import os
from types import SimpleNamespace

# Use the bundled model-cost map; skip the network fetch LiteLLM does on import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

from Axoscode import config
from Axoscode.tools import TOOL_SCHEMAS, TOOLS

litellm.drop_params = True  # ignore params a given provider doesn't support

MAX_TOKENS = 8192


# ------------------------------------------------------------------ helpers

def get_model():
    """LiteLLM model id: 'provider/model' (provider prefixed if missing)."""
    model = config.MODEL
    return model if "/" in model else f"{config.PROVIDER}/{model}"


def _use_responses():
    return config.API_FORMAT == "responses"


def _common():
    kwargs = {"model": get_model()}
    if config.API_KEY:
        kwargs["api_key"] = config.API_KEY
    if config.BASE_URL:
        kwargs["api_base"] = config.BASE_URL
    return kwargs


def _clean_tools(tools):
    """Chat-format tool schemas without Axos-only keys (is_concurrency_safe)."""
    cleaned = []
    for schema in tools or TOOL_SCHEMAS:
        function = {
            k: v for k, v in schema["function"].items() if k != "is_concurrency_safe"
        }
        cleaned.append({"type": "function", "function": function})
    return cleaned


def _responses_tools(tools):
    return [{"type": "function", **t["function"]} for t in _clean_tools(tools)]


def _responses_input(messages):
    """Chat-format history -> Responses API input items."""
    items = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m["tool_call_id"],
                "output": m.get("content") or "",
            })
            continue
        if m.get("content"):
            items.append({"role": role, "content": m["content"]})
        for call in m.get("tool_calls") or []:
            fn = call["function"]
            items.append({
                "type": "function_call",
                "call_id": call["id"],
                "name": fn["name"],
                "arguments": fn["arguments"],
            })
    return items


def _make_call(call_id, name, arguments):
    """Tool-call object shaped like the OpenAI SDK's (what agent/tools expect)."""
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments or ""),
    )


def _usage(u):
    """Normalise chat (prompt_tokens) and Responses (input_tokens) usage."""
    if u is None:
        return None

    def get(obj, *names):
        for n in names:
            v = getattr(obj, n, None)
            if v is None and isinstance(obj, dict):
                v = obj.get(n)
            if v is not None:
                return v
        return 0

    prompt = get(u, "prompt_tokens", "input_tokens")
    completion = get(u, "completion_tokens", "output_tokens")
    out_d = get(u, "completion_tokens_details", "output_tokens_details")
    in_d = get(u, "prompt_tokens_details", "input_tokens_details")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": (get(out_d, "reasoning_tokens") if out_d else 0) or 0,
        "cached_tokens": (get(in_d, "cached_tokens") if in_d else 0) or 0,
        "total_tokens": get(u, "total_tokens") or prompt + completion,
    }


# --------------------------------------------------------------- sync calls

def call_llm(messages, tools=None):
    """One blocking call. Returns (message, usage) where message has
    .content, .tool_calls and .model_dump()."""
    if _use_responses():
        response = litellm.responses(
            input=_responses_input(messages),
            tools=_responses_tools(tools),
            tool_choice="auto",
            max_output_tokens=MAX_TOKENS,
            **_common(),
        )
        text, calls = [], []
        for item in response.output or []:
            kind = getattr(item, "type", None)
            if kind == "message":
                text += [
                    c.text for c in item.content if getattr(c, "text", None)
                ]
            elif kind == "function_call":
                calls.append(_make_call(item.call_id, item.name, item.arguments))
        content = "".join(text) or None
        message = _Message(content, calls)
        return message, _usage(response.usage)

    response = litellm.completion(
        messages=messages,
        tools=_clean_tools(tools),
        tool_choice="auto",
        max_tokens=MAX_TOKENS,
        **_common(),
    )
    return response.choices[0].message, _usage(response.usage)


class _Message:
    """Minimal assistant message for the Responses path."""

    def __init__(self, content, tool_calls):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or None

    def model_dump(self, exclude_none=False):
        data = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                    },
                }
                for c in self.tool_calls
            ]
        if exclude_none:
            data = {k: v for k, v in data.items() if v is not None}
        return data


def complete_text(messages):
    """Plain completion with no tools (used by compaction)."""
    if _use_responses():
        response = litellm.responses(
            input=_responses_input(messages), **_common()
        )
        return "".join(
            c.text
            for item in response.output or []
            if getattr(item, "type", None) == "message"
            for c in item.content
            if getattr(c, "text", None)
        )
    response = litellm.completion(messages=messages, **_common())
    return response.choices[0].message.content


# ------------------------------------------------------------ streaming call

async def call_llm_stream(messages, tools=None):
    """Stream a model turn, yielding SSE-style events:

        {"type": "content_block_start", "index": int}
        {"type": "content_block_delta", "index": int, "text": str}
        {"type": "content_block_stop", "index": int}
        {"type": "tool_call", "call": <ToolCall>}
        {"type": "usage", ...}
    """
    if _use_responses():
        async for event in _stream_responses(messages, tools):
            yield event
    else:
        async for event in _stream_chat(messages, tools):
            yield event


async def _stream_chat(messages, tools):
    stream = await litellm.acompletion(
        messages=messages,
        tools=_clean_tools(tools),
        tool_choice="auto",
        max_tokens=MAX_TOKENS,
        stream=True,
        stream_options={"include_usage": True},
        **_common(),
    )

    usage = None
    index = 0
    calls = {}

    async for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta

        if delta.content:
            yield {"type": "content_block_start", "index": index}
            yield {"type": "content_block_delta", "index": index, "text": delta.content}
            yield {"type": "content_block_stop", "index": index}
            index += 1

        for part in delta.tool_calls or []:
            slot = calls.setdefault(
                part.index, {"id": None, "name": "", "arguments": ""}
            )
            if part.id:
                slot["id"] = part.id
            if part.function:
                if part.function.name:
                    slot["name"] = part.function.name
                if part.function.arguments:
                    slot["arguments"] += part.function.arguments

    for i in sorted(calls):
        c = calls[i]
        yield {"type": "tool_call", "call": _make_call(c["id"], c["name"], c["arguments"])}

    if usage:
        yield {"type": "usage", **_usage(usage)}


async def _stream_responses(messages, tools):
    stream = await litellm.aresponses(
        input=_responses_input(messages),
        tools=_responses_tools(tools),
        tool_choice="auto",
        max_output_tokens=MAX_TOKENS,
        stream=True,
        **_common(),
    )

    index = 0
    usage = None

    if not hasattr(stream, "__aiter__"):
        # Provider/proxy ignored stream=True and returned a whole response.
        for item in stream.output or []:
            kind = getattr(item, "type", None)
            if kind == "message":
                for c in item.content:
                    if getattr(c, "text", None):
                        yield {"type": "content_block_start", "index": index}
                        yield {"type": "content_block_delta", "index": index, "text": c.text}
                        yield {"type": "content_block_stop", "index": index}
                        index += 1
            elif kind == "function_call":
                yield {"type": "tool_call", "call": _make_call(item.call_id, item.name, item.arguments)}
        if stream.usage:
            yield {"type": "usage", **_usage(stream.usage)}
        return

    async for event in stream:
        kind = getattr(event, "type", "")

        if kind == "response.output_text.delta" and event.delta:
            yield {"type": "content_block_start", "index": index}
            yield {"type": "content_block_delta", "index": index, "text": event.delta}
            yield {"type": "content_block_stop", "index": index}
            index += 1

        elif kind == "response.output_item.done":
            item = event.item
            if getattr(item, "type", None) == "function_call":
                yield {
                    "type": "tool_call",
                    "call": _make_call(item.call_id, item.name, item.arguments),
                }

        elif kind == "response.completed":
            usage = getattr(event.response, "usage", None)

    if usage:
        yield {"type": "usage", **_usage(usage)}


if __name__ == "__main__":
    user_input = input("Enter your prompt: ")

    messages = [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": user_input},
    ]

    message, usage = call_llm(messages)

    print("\nAgent :", message.content, "\n")

    for tool_call in message.tool_calls or []:
        args = json.loads(tool_call.function.arguments)
        result = TOOLS[tool_call.function.name](**args)
        print("\nTool :", tool_call.function.name, "\n")
        print("Result :", result, "\n")

    print(usage)
