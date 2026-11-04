import json

from openai import OpenAI

from Axoscode import config
from Axoscode.tools import TOOL_SCHEMAS, TOOLS

_client = None


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
