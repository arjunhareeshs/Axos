import os
import json
import asyncio
import subprocess
import threading
import time

from . import browser
from . import config
from . import documents
from . import history
from . import sandbox
from . import workspace
from .permissions import check
from .subagent import SPAWN_SCHEMA, spawn_agents
from .skills import read_skill
from .todos import TODO_SCHEMA ,write_todos

# Parallel subagents share one terminal: only one approval prompt at a time.
APPROVAL_LOCK = threading.Lock()


def bash(command: str) -> str:
    try:
        result  = sandbox.run(command)
    except subprocess.TimeoutExpired as expired:
        return (
            f"timed out after {expired.timeout}s and then was killed. "
            "narrow it down - search inside the working directory rather than /."
        )
    return history.cap((result.stdout +result.stderr) or "(no output)")

def read_file(path:str) -> str:
    return history.cap(documents.read(workspace.resolve(path)))

def write_file(path:str , content:str) -> str:
    with open(workspace.resolve(path), "w") as f:
        f.write(content)
    return  f"Wrote {path}"

def str_replace(path ,old_str ,new_str ,allow_multi_edit = False):
    target = workspace.resolve(path)
    with open(target) as f:
        content = f.read()

    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str was not found in {path}"
    if count > 1 and not allow_multi_edit:
        return (
            f"Error: old_str matches {count} times in {path}. "
            "Add surrounding lines to make it unique, "
            "or set allow_multi_edit to replace them all."
        )

    with open(target, "w") as f:
        f.write(content.replace(old_str, new_str))
    return f"Replaced {count} match(es) in {path}"

def execute(tool_call):
    """Run one tool call through the permission layer.

    Shared by the main loop and by subagents, so a subagent is fenced in by
    exactly the same rules - it is not a way around them.

    A tool call is text the model wrote, so all of it is untrusted: the name
    may not exist, the arguments may not be JSON, and they may not match the
    signature. Every one of those comes back as a result the model can read
    and retry. None of them is allowed to end the session.
    """
    from .ui import ui

    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as broken:
        return {}, f"Error: arguments were not valid JSON ({broken})."

    if name not in TOOLS:
        return args, f"Error: no tool named '{name}'. Available: {', '.join(TOOLS)}."

    try:
        action, reason = check(name, args)
        if action == "deny":
            return args, f"Blocked by policy: {reason}"
        if action == "ask":
            with APPROVAL_LOCK:
                approved = ui.approve(reason)
            if not approved:
                return args, "The user denied this tool call."
        result = TOOLS[name](**args)
        time.sleep(config.TOOL_DELAY_MS / 1000)  # a short, visible pause between actions
        return args, result
    except TypeError as mismatch:
        return args, f"Error: wrong arguments for {name} ({mismatch})."
    except KeyError as missing:
        return args, f"Error: {name} needs an argument you did not send: {missing}."
    except Exception as failure:  # noqa: BLE001 - the model gets to see and retry
        return args, f"Error: {name} failed - {type(failure).__name__}: {failure}"


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return its combined stdout and stderr.",
            "is_concurrency_safe": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents as text. Also reads PDF, Word (.docx) and image files: text, tables (as Markdown) and descriptions of images.",
            "is_concurrency_safe": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Open a skill by name and return its full instructions.",
            "is_concurrency_safe": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the skill to open",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a file, or overwrite it if it already exists.",
            "is_concurrency_safe": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to write"},
                    "content": {"type": "string", "description": "The full contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace",
                "description": (
                    "Replace exact text in a file. old_str must appear exactly once, "
                    "so include surrounding lines if needed."
                ),
                "is_concurrency_safe": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit"},
                    "old_str": {"type": "string", "description": "Exact text to find"},
                    "new_str": {"type": "string", "description": "Text to put in its place"},
                    "allow_multi_edit": {
                        "type": "boolean",
                        "description": "Replace every match instead of failing",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    TODO_SCHEMA,
    SPAWN_SCHEMA,
    *browser.SCHEMAS,
]

TOOLS = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "str_replace": str_replace,
    "read_skill": read_skill,
    "write_todos": write_todos,
    "spawn_agents": spawn_agents,
    **browser.TOOLS,
}


def tools_prompt():
    """System-prompt section naming every tool, generated from the registry
    so it cannot drift from what the model is actually given."""
    lines = []
    for schema in TOOL_SCHEMAS:
        function = schema["function"]
        summary = function["description"].split(". ")[0].rstrip(".")
        lines.append(f"- `{function['name']}`: {summary}.")
    return "\n\n# Tool reference\nEvery tool you have:\n" + "\n".join(lines)


def partition_tool_calls(tool_calls, safety_map):
    """Single-pass reduce: group consecutive concurrency-safe calls into one
    batch; every unsafe call gets its own solo batch.

    Returns a list of batches, each batch a list of tool_call objects.
    Safe batches may run in parallel; unsafe batches run one at a time.
    """
    batches = []
    current = []

    for call in tool_calls:
        name = call.function.name
        safe = safety_map.get(name, False)

        if safe and current and config_safe(current[0]):
            current.append(call)
        else:
            if current:
                batches.append(current)
            current = [call]

    if current:
        batches.append(current)

    return batches


def config_safe(call):
    return config.TOOL_CONCURRENCY_SAFE.get(call.function.name, False)


async def execute_async(tool_call):
    """Run one tool call through execute() in a thread so it won't block."""
    return await asyncio.to_thread(execute, tool_call)
