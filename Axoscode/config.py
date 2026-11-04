import os
from pathlib import Path

ENV_FILE = Path.home() / ".agents" / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

# Prefer plain names from .env; fall back to AXOS_* then defaults.
BASE_URL = (
    os.environ.get("BASE_URL")
    or os.environ.get("AXOS_BASE_URL")
    or "https://api.openai.com/v1"
)
API_KEY = os.environ.get("API_KEY") or os.environ.get("AXOS_API_KEY") or ""
MODEL = (
    os.environ.get("MODEL")
    or os.environ.get("AXOS_MODEL")
    or "gpt-4o-mini"
)

CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", 128_000))

# Fractions of CONTEXT_WINDOW — history.fit / compact multiply these.
COMPACT_AT = 0.85
COMPACT_TO = 0.35
