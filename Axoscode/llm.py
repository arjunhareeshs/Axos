import json
import os

from openai import OpenAI
from Axoscode import config
from Axoscode.tools import TOOL_SCHEMAS, TOOLS

client = OpenAI(
    base_url=config.BASE_URL,
    api_key=config.API_KEY,
    )

SYSTEM_PROMPT = """
  
"""

def call_llm(messages , tools = None):
    response = client.chat.completions.create(
        model=config.MODEL,
        messages=messages,
        tools=tools or TOOL_SCHEMAS,
        tool_choice="auto",
        max_tokens=config.CONTEXT_WINDOW,
    )
    message = response.choices[0].message

    usage = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "reasoning_tokens": getattr(getattr(response.usage, "completion_tokens_details", None), "reasoning_tokens", 0),
        "cached_tokens": getattr(getattr(response.usage, "prompt_tokens_details", None), "cached_tokens", 0),
        "total_tokens": response.usage.total_tokens,
    }

    return message , usage

if __name__ == "__main__":
    user_input = input("Enter your prompt: ")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input}
    ]

    message , usage = call_llm(messages)

    print("\nAgent :", message.content, "\n")

    for tool_call in message.tool_calls or []:
        args = json.loads(tool_call.function.arguments)
        result = TOOLS[tool_call.function.name](**args)
        print("\nTool :", tool_call.function.name, "\n")
        print("Result :", result, "\n")

    print(usage)