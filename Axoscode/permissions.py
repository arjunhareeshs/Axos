"""Which tool calls need a human.

The sandbox decides what is *possible*. These rules only decide what is worth
interrupting you for - so read-only commands run silently, and the risky ones
still stop and ask.
"""

from fnmatch import fnmatch
from . import workspace

# Last matching rule wins, so put the catch-all first.
BASH_RULES = {
    "*": "ask",
    # read-only: let them through
    "ls*": "allow",
    "pwd": "allow",
    "cd *": "allow",
    "echo *": "allow",
    "sort*": "allow",
    "uniq*": "allow",
    "cut *": "allow",
    "basename *": "allow",
    "dirname *": "allow",
    "date*": "allow",
    "env": "allow",
    "cat *": "allow",
    "head *": "allow",
    "tail *": "allow",
    "wc *": "allow",
    "file *": "allow",
    "which *": "allow",
    "grep *": "allow",
    "rg *": "allow",
    "find *": "allow",
    "tree*": "allow",
    "git status*": "allow",
    "git diff*": "allow",
    "git log*": "allow",
    "git show*": "allow",
    "git ls-files*": "allow",
    "pytest*": "allow",
    "python -m pytest*": "allow",
    # risky: never, even if the user says yes
    "rm *": "deny",
    "sudo *": "deny",
    "chmod *": "deny",
    "chown *": "deny",
    "curl *": "deny",
    "wget *": "deny",
    "git push*": "deny",
    "git reset*": "deny",
    "git clean*": "deny",
}

def split_command(command):
    """Split a compound command on the separators that actually separate.

    A naive split on | and ; also cuts inside quotes, so `rg "cap|max"` breaks
    into fragments that match no rule and fall through to "ask". Anything
    quoted or backslash-escaped is an argument, not a separator.
    """
    parts, current, quote = [], [], None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            quote = None if char == quote else quote
        elif char == "\\":
            current.append(char)
            index += 1
            if index < len(command):
                current.append(command[index])
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in "&|;":
            parts.append("".join(current))
            current = []
            while index + 1 < len(command) and command[index + 1] in "&|":
                index += 1
        else:
            current.append(char)
        index += 1

    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def decide(command):
    """Rate every part of a compound command; the strictest verdict wins."""
    verdicts = []
    for part in split_command(command):
        action = "ask"
        for pattern, rule in BASH_RULES.items():
            if fnmatch(part, pattern):
                action = rule
        verdicts.append(action)

    for strictest in ("deny", "ask"):
        if strictest in verdicts:
            return strictest
    return "allow"


def inside_workspace(path):
    """Is the path inside the caller's working tree (project or worktree)?"""
    root = workspace.current()
    target = workspace.resolve(path).resolve()
    return target == root or root in target.parents


def check(name, args):
    """Return (action, reason). Action is allow, ask or deny."""
    if name == "bash":
        return decide(args["command"]), f"run: {args['command']}"

    if name in ("write_file", "str_replace") and not inside_workspace(args["path"]):
        return "ask", f"{name} outside {workspace.current()}: {args['path']}"

    # The browser reaches the network, which shell commands may not. Opening
    # one is your decision; so is sending a local file to a web page.
    if name == "browser_create_session":
        return "ask", "open a web browser (gives the agent access to the internet)"
    if name == "browser_upload":
        return "ask", f"upload to a web page: {', '.join(args.get('paths') or [])}"

    return "allow", None