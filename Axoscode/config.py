import os
from pathlib import Path

# The checkout's own .env first, then the per-user one. Real environment
# variables win over both.
ENV_FILES = [
    Path(__file__).resolve().parent.parent / ".env",
    Path.home() / ".agents" / ".env",
]

for env_file in ENV_FILES:
    if not env_file.exists():
        continue
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _env(*names, default=""):
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return default


# Provider loader (LiteLLM). MODEL may be a full LiteLLM id such as
# "anthropic/claude-sonnet-4-5"; a bare id gets "<PROVIDER>/" prefixed.
PROVIDER = _env("PROVIDER", "AXOS_PROVIDER")  # empty: MODEL carries it, else openai
MODEL = _env("MODEL", "AXOS_MODEL")  # required, no default: set it in .env
# None lets LiteLLM use each provider's own default endpoint / env key.
BASE_URL = _env("BASE_URL", "AXOS_BASE_URL") or None
API_KEY = _env("API_KEY", "AXOS_API_KEY") or None
# "chat" -> chat/completions, "responses" -> the Responses API.
API_FORMAT = _env("API_FORMAT", "AXOS_API_FORMAT", default="chat").lower()
# Prompt caching: stable prefix + cache markers. Set PROMPT_CACHE=0 to disable.
PROMPT_CACHE = _env("PROMPT_CACHE", "AXOS_PROMPT_CACHE", default="1") != "0"

# Images inside documents are described by a vision model. VISION_MODEL falls
# back to MODEL; MAX_DOC_IMAGES bounds the calls made per document.
VISION_MODEL = _env("VISION_MODEL", "AXOS_VISION_MODEL")
MAX_DOC_IMAGES = int(_env("MAX_DOC_IMAGES", "AXOS_MAX_DOC_IMAGES", default="10"))

# Fault tolerance: retries for transient API failures, and the longest single
# wait (seconds). Rate limits that reset later than this stop with a message.
MAX_RETRIES = int(_env("MAX_RETRIES", "AXOS_MAX_RETRIES", default="5"))
MAX_RETRY_WAIT = int(_env("MAX_RETRY_WAIT", "AXOS_MAX_RETRY_WAIT", default="60"))

# Pause after every tool call, in milliseconds, so each step can be followed
# on screen. 0 turns it off.
TOOL_DELAY_MS = int(_env("TOOL_DELAY_MS", "AXOS_TOOL_DELAY_MS", default="500"))

# Browser tools (Playwright). Headless "auto" shows a window when a display
# exists. Timeouts are per action and per navigation, in milliseconds.
_headless = _env("BROWSER_HEADLESS", "AXOS_BROWSER_HEADLESS", default="auto").lower()
BROWSER_HEADLESS = (
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if _headless == "auto"
    else _headless in ("1", "true", "yes")
)
BROWSER_TIMEOUT_MS = int(_env("BROWSER_TIMEOUT_MS", "AXOS_BROWSER_TIMEOUT_MS", default="15000"))
BROWSER_NAV_TIMEOUT_MS = int(_env("BROWSER_NAV_TIMEOUT_MS", "AXOS_BROWSER_NAV_TIMEOUT_MS", default="30000"))

# Top-level instructions for the model. Override with your own file via
# SYSTEM_PROMPT_FILE; the default ships with the package.
SYSTEM_PROMPT_FILE = Path(
    _env("SYSTEM_PROMPT_FILE", "AXOS_SYSTEM_PROMPT_FILE")
    or Path(__file__).resolve().parent / "prompts" / "system.md"
).expanduser()

# Token budget for the model's context window, and the cap on each reply.
CONTEXT_WINDOW = int(_env("CONTEXT_WINDOW", "AXOS_CONTEXT_WINDOW", default="128000"))
MAX_TOKENS = int(_env("MAX_TOKENS", "AXOS_MAX_TOKENS", default="8192"))

# Fractions of CONTEXT_WINDOW — history.fit / compact multiply these.
COMPACT_AT = 0.85
COMPACT_TO = 0.35

MAX_TURNS = int(_env("MAX_TURNS", "AXOS_MAX_TURNS", default="100"))  # model calls per user prompt
MAX_SPAWN = 3           # subagents per spawn_agents call
CHILD_MAX_TURNS = int(_env("CHILD_MAX_TURNS", "AXOS_CHILD_MAX_TURNS", default="30"))  # per subagent
STUCK_LOOP_THRESHOLD = 3

TOOL_CONCURRENCY_SAFE = {
    "read_file": True,
    "read_skill": True,
    "bash": False,
    "write_file": False,
    "str_replace": False,
    "write_todos": False,
    "spawn_agents": False,
}
