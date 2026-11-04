import json

from openai import AsyncOpenAI, OpenAI

from Axoscode import config
from Axoscode.tools import TOOL_SCHEMAS, TOOLS

_client = None
_async_client = None


def get_client():
    """Build the OpenAI client on first use so imports work without a key."""
    global _client
    if _client is None:
        if not config.API_KEY:
            raise RuntimeError(
                "No API key set. Put API_KEY in ~/.agents/.env "
                "or export AXOS_API_KEY / API_KEY."
            )
        _client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)
    return _client


def get_async_client():
    """Build the async OpenAI client on first use."""
    global _async_client
    if _async_client is None:
        if not config.API_KEY:
            raise RuntimeError(
                "No API key set. Put API_KEY in ~/.agents/.env "
                "or export AXOS_API_KEY / API_KEY."
            )
        _async_client = AsyncOpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)
    return _async_client


# Back-compat for compact.py (`from .llm import client`) and older call sites.
class _ClientProxy:
    def __getattr__(self, name):
        return getattr(get_client(), name)


client = _ClientProxy()


def call_llm(messages, tools=None):
    response = get_client().chat.completions.create(
        model=config.MODEL,
        messages=messages,
        tools=tools or TOOL_SCHEMAS,
        tool_choice="auto",
        max_tokens=8192,
    )
    message = response.choices[0].message

    usage = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "reasoning_tokens": getattr(
            getattr(response.usage, "completion_tokens_details", None),
            "reasoning_tokens",
            0,
        )
        or 0,
        "cached_tokens": getattr(
            getattr(response.usage, "prompt_tokens_details", None),
            "cached_tokens",
            0,
        )
        or 0,
        "total_tokens": response.usage.total_tokens,
    }

    return message, usage


async def call_llm_stream(messages, tools=None):
    """Stream chat completions, yielding SSE-style events.

    Events:
        {"type": "content_block_start", "index": int}
        {"type": "content_block_delta", "index": int, "text": str}
        {"type": "content_block_stop", "index": int}
        {"type": "tool_call", "call": <ToolCall>}
        {"type": "usage", ...}
    """
    client = get_async_client()
    stream = await client.chat.completions.create(
        model=config.MODEL,
        messages=messages,
        tools=tools or TOOL_SCHEMAS,
        tool_choice="auto",
        max_tokens=8192,
        stream=True,
    )

    usage = None
    content_block_index = 0
    tool_call_accumulator = {}

    async for chunk in stream:
        if not chunk.choices:
            if chunk.usage:
                usage = chunk.usage
            continue

        delta = chunk.choices[0].delta

        if delta.content:
            yield {
                "type": "content_block_start",
                "index": content_block_index,
            }
            yield {
                "type": "content_block_delta",
                "index": content_block_index,
                "text": delta.content,
            }
            yield {
                "type": "content_block_stop",
                "index": content_block_index,
            }
            content_block_index += 1

        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_call_accumulator:
                    tool_call_accumulator[idx] = tc_delta
                else:
                    existing = tool_call_accumulator[idx]
                    if tc_delta.id:
                        existing.id = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        existing.function.name = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        existing.function.arguments = (
                            (existing.function.arguments or "")
                            + tc_delta.function.arguments
                        )

    for idx in sorted(tool_call_accumulator.keys()):
        yield {"type": "tool_call", "call": tool_call_accumulator[idx]}

    if usage:
        yield {
            "type": "usage",
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "reasoning_tokens": getattr(
                getattr(usage, "completion_tokens_details", None),
                "reasoning_tokens",
                0,
            )
            or 0,
            "cached_tokens": getattr(
                getattr(usage, "prompt_tokens_details", None),
                "cached_tokens",
                0,
            )
            or 0,
        }


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
