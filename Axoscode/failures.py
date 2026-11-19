"""Surviving model-API failures, and telling the user what happened.

Every failure is classified. Transient ones (rate limits that reset soon,
timeouts, 5xx) are retried with backoff, and each wait is shown. The rest
(out of credits, bad key, a daily limit, an oversized request) stop at once
with a message that says what to do. Callers only ever see LLMError.
"""

import asyncio
import random
import re
import time

import litellm

from . import config

LIMIT_NAMES = {
    "RPM": "requests per minute",
    "RPD": "requests per day",
    "TPM": "tokens per minute",
    "TPD": "tokens per day",
}
QUOTA = re.compile(
    r"insufficient_quota|no credits|credit balance|exceeded your current quota"
    r"|billing|payment required",
    re.I,
)
LIMIT = re.compile(r"\((RPM|RPD|TPM|TPD)\)")
DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


class LLMError(Exception):
    """A model-API failure, worded for the user. `kind` says what it was:
    quota, rate_limit, auth, context, request, transient or unknown."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def classify(failure):
    """-> (kind, rate-limit name or None)."""
    text = str(failure)
    status = getattr(failure, "status_code", None)

    if QUOTA.search(text):
        return "quota", None
    if isinstance(failure, litellm.RateLimitError) or status == 429:
        found = LIMIT.search(text)
        return "rate_limit", found.group(1) if found else None
    if isinstance(failure, (litellm.AuthenticationError, litellm.PermissionDeniedError)):
        return "auth", None
    if isinstance(failure, litellm.ContextWindowExceededError):
        return "context", None
    if isinstance(failure, (litellm.BadRequestError, litellm.NotFoundError)):
        return "request", None
    if isinstance(
        failure,
        (
            litellm.Timeout,
            litellm.APIConnectionError,
            litellm.ServiceUnavailableError,
            litellm.InternalServerError,
        ),
    ) or (status or 0) >= 500:
        return "transient", None
    return "unknown", None


def retry_after(failure):
    """Seconds the provider says to wait, from its headers or its message."""
    headers = getattr(getattr(failure, "response", None), "headers", None) or {}
    try:
        if "retry-after-ms" in headers:
            return float(headers["retry-after-ms"]) / 1000
        if "retry-after" in headers:
            return float(headers["retry-after"])
    except ValueError:
        pass
    said = re.search(r"try again in ([\w.]+)", str(failure), re.I)
    if said:
        parts = DURATION.findall(said.group(1))
        if parts:
            return sum(float(n) * UNITS[unit] for n, unit in parts)
    return None


def _duration(seconds):
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def detail(failure):
    text = " ".join(str(failure).split())
    text = re.sub(r"^litellm\.\w+: (\w+: )?", "", text)
    return text[:240]


def _message(kind, limit, failure, model, wait):
    if kind == "quota":
        return (
            f"Out of API credits for {model}. Add credits or raise the spending "
            "limit with your provider, then send your message again."
        )
    if kind == "rate_limit":
        name = LIMIT_NAMES.get(limit, "rate")
        reset = f" Resets in about {_duration(wait)}." if wait else ""
        return f"Rate limit reached ({name}) for {model}.{reset} Try again then."
    if kind == "auth":
        return f"The API key was rejected for {model}. Check the key in .env."
    if kind == "context":
        return (
            f"The conversation no longer fits {model}'s context window. "
            "Run /compact, then try again."
        )
    if kind == "request":
        return f"The provider rejected the request for {model}: {detail(failure)}"
    if kind == "transient":
        return (
            f"{model} was unreachable or failing after {config.MAX_RETRIES} "
            f"retries ({detail(failure)}). Try again shortly."
        )
    return f"{type(failure).__name__}: {detail(failure)}"


def decide(failure, attempt, model):
    """Seconds to wait before retrying, or raise LLMError to stop."""
    if isinstance(failure, LLMError):
        raise failure

    kind, limit = classify(failure)
    wait = retry_after(failure)
    retryable = kind in ("rate_limit", "transient") and attempt < config.MAX_RETRIES
    if kind == "rate_limit":
        # A daily limit, or a reset further off than we will sit waiting.
        retryable = retryable and limit not in ("RPD", "TPD")
        retryable = retryable and (wait or 0) <= config.MAX_RETRY_WAIT

    if not retryable:
        raise LLMError(kind, _message(kind, limit, failure, model, wait)) from failure

    delay = wait if wait is not None else min(2**attempt + random.random(), config.MAX_RETRY_WAIT)
    delay += 0.25  # land just after the reset, not on it
    reason = (
        f"Rate limit ({LIMIT_NAMES.get(limit, 'rate')})" if kind == "rate_limit"
        else "Provider error"
    )
    _alert(f"{reason} on {model}. Retrying in {_duration(delay)} "
           f"(attempt {attempt + 1}/{config.MAX_RETRIES}).")
    return delay


def _alert(text):
    from .ui import ui

    ui.alert(text)


def with_retries(call, model):
    """Run a blocking call, retrying transient failures."""
    attempt = 0
    while True:
        try:
            return call()
        except Exception as failure:  # noqa: BLE001 - classified in decide()
            delay = decide(failure, attempt, model)
        time.sleep(delay)
        attempt += 1


async def with_retries_async(call, model):
    """Awaitable counterpart of with_retries."""
    attempt = 0
    while True:
        try:
            return await call()
        except Exception as failure:  # noqa: BLE001
            delay = decide(failure, attempt, model)
        await asyncio.sleep(delay)
        attempt += 1
