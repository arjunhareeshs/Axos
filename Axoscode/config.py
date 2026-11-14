import os
from pathlib import Path

ENV_FILE = Path.home() / ".agents" / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

def _env(*names, default=""):
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return default


# Provider loader (LiteLLM). MODEL may be a full LiteLLM id such as
# "anthropic/claude-sonnet-4-5"; a bare id gets "<PROVIDER>/" prefixed.
PROVIDER = _env("PROVIDER", "AXOS_PROVIDER", default="openai")
MODEL = _env("MODEL", "AXOS_MODEL", default="gpt-4o-mini")
# None lets LiteLLM use each provider's own default endpoint / env key.
BASE_URL = _env("BASE_URL", "AXOS_BASE_URL") or None
API_KEY = _env("API_KEY", "AXOS_API_KEY") or None
# "chat" -> chat/completions, "responses" -> the Responses API.
API_FORMAT = _env("API_FORMAT", "AXOS_API_FORMAT", default="chat").lower()

CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", 128_000))

# Fractions of CONTEXT_WINDOW — history.fit / compact multiply these.
COMPACT_AT = 0.85
COMPACT_TO = 0.35

MAX_TURNS = 50
STUCK_LOOP_THRESHOLD = 3

TOOL_CONCURRENCY_SAFE = {
    "read_file": True,
    "read_skill": True,
    "bash": False,
    "write_file": False,
    "str_replace": False,
    "write_todos": False,
    "task": False,
}
