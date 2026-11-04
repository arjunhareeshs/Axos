import os

BASE_URL = os.environ.get("AXOS_BASE_URL", "https://api.openai.com/v1")
API_KEY = os.environ.get("AXOS_API_KEY", "")
MODEL = os.environ.get("AXOS_MODEL", "gpt-4o-mini")

CONTEXT_WINDOW = 128_000
COMPACT_AT = 100_000
COMPACT_TO = 50_000
