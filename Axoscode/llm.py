import json
import os

from openai import OpenAI
from Axoscode import config
from Axoscode.tools import TOOL_SCHEMAS

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
        compact_at=config.COMPACT_AT,
        compact_to=config.COMPACT_TO,
    )
    messages = messages.choices[0].message

    completion_details = response.usage.completion_tokens
    prompt_details = response.usage.prompt_tokens
    
    usage = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "reasoning_tokens": response.usage.reasoning_tokens,
        "cached_tokens": response.usage.cached_tokens,
        "total_tokens": response.usage.total_tokens,
    }

    return messages , usage

if __name__ == "__main__":
    user_input = input("Enter your prompt: ")

    message ,usage = call_llm([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input}
    ])

    print("\nAgent :",message.content, "\n")

    if message.tool_calls:
        tool_call = message.tool_calls[0]
        args = json.loads(tool_call.function.arguments)
        resut = TOOLS[tool_call.function.name](**args)
        print("\nTool :",tool_call.function.name, "\n")

    print(usage)