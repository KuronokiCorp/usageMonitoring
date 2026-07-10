#!/usr/bin/env python3
"""
iterm_ai - decide whether an iTerm2 session should be nudged to "continue".

Given a snapshot of a session's screen, it returns one of:
  - "continue": the session is idle/paused (e.g. was interrupted, or hit a usage
                limit that has since reset) and a "continue" nudge would resume it.
  - "wait":     the session hit a usage limit whose reset time is still in the
                future — do not nudge yet.
  - "skip":     the session is actively working, sitting at a healthy prompt with
                nothing pending, or blocked on a human decision a nudge won't fix.

Two backends:
  - MiniMax (preferred): calls MiniMax's OpenAI-compatible chat-completions API
    over plain HTTP (stdlib urllib — no pip install). Configure with env vars:
        MINIMAX_API_KEY    required, your MiniMax API key
        MINIMAX_MODEL      default "MiniMax-M2.7"
        MINIMAX_BASE_URL   default "https://api.minimax.io/v1" (international
                           platform; China mainland: "https://api.minimax.chat/v1")
  - Heuristic (fallback): pure-stdlib pattern matching, always available, used when
    MINIMAX_API_KEY is unset or a MiniMax call fails.

`judge()` never raises — on any error it degrades to the heuristic.
"""
import json
import os
import re
import urllib.request

DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/")

_SYSTEM = """You monitor a terminal session (often running the Claude Code CLI) and \
decide whether to send it a "continue" nudge to resume work. You are given the visible \
screen contents. Classify into exactly one action:

- "continue": the session appears idle, paused, or waiting after an interruption, OR it \
hit a usage/rate limit that has already reset, and a short "continue" message would get \
it working again. Also use this when it finished a step and is clearly waiting to proceed.
- "wait": the session shows a usage/rate limit whose reset time is still in the future \
(e.g. "resets at 5pm", "try again in 2 hours"). Do not nudge yet.
- "skip": the session is actively working (a spinner, "thinking", running a tool, \
streaming tokens), sitting at a healthy empty prompt with nothing pending, or blocked on \
a human decision (a permission/confirmation prompt) that a generic "continue" would not \
resolve.

Prefer "skip" when unsure — a wrong "continue" types into a working agent.

Respond with ONLY a JSON object, no prose and no markdown fences, of the form:
{"action": "continue" | "wait" | "skip", "reason": "<one short sentence>"}"""


# --------------------------------------------------------------------------- #
# Heuristic backend (stdlib only)
# --------------------------------------------------------------------------- #
# Signs the agent is busy — never nudge these.
_BUSY = re.compile(
    r"esc to interrupt|still thinking|tokens|Recombobulating|Seasoning|Razzmatazzing|"
    r"Running \d+ shell|running…|working…|✻|✳ .*…|⏳|\btokens\b|↓ [\d.]+k",
    re.IGNORECASE,
)
# A usage/rate limit the session has ACTUALLY hit (not a banner merely mentioning
# the phrase "usage limit"). Requires an explicit hit/reached/exceeded signal.
_LIMIT_HIT = re.compile(
    r"you'?ve\s+(?:hit|reached|used up)\b|you have\s+(?:hit|reached|used up)\b|"
    r"\blimit reached\b|\blimit exceeded\b|out of (?:usage|credits)|"
    r"no (?:usage|credits) (?:left|remaining)|rate[- ]limited",
    re.IGNORECASE,
)
# A usage/rate limit with a future reset -> wait.
_LIMIT_FUTURE = re.compile(
    r"(reset|resets|resets? at|try again|available again|back at|in \d+\s*(?:min|hour|hr))",
    re.IGNORECASE,
)
# A human decision the nudge won't resolve.
_ASKING = re.compile(
    r"\b(1[.):]\s*Yes|Do you want to proceed|Allow\?|\[y/N\]|approve this|permission)\b",
    re.IGNORECASE,
)


def heuristic(contents: str) -> dict:
    """Conservative rule-based fallback. Favors 'skip' — it only nudges on an
    explicit limit-hit or an interrupted/paused signal, never on a bare idle
    prompt (too many healthy sessions sit at an empty prompt normally). For
    accurate idle-vs-working judgment, use the Claude backend."""
    lines = contents.splitlines()[-40:]
    tail = "\n".join(lines)
    if _BUSY.search(tail):
        return {"action": "skip", "reason": "session appears to be actively working"}
    if _LIMIT_HIT.search(tail):
        if _LIMIT_FUTURE.search(tail):
            return {"action": "wait", "reason": "usage limit hit with a future reset shown"}
        return {"action": "continue", "reason": "session hit a usage limit with no pending reset"}
    if _ASKING.search(tail):
        return {"action": "skip", "reason": "session is waiting on a human decision"}
    if re.search(r"\binterrupted\b|\bpaused\b|waiting for input|press .* to continue", tail, re.IGNORECASE):
        return {"action": "continue", "reason": "session shows an interrupted/paused state"}
    return {"action": "skip", "reason": "no clear limit-hit or interrupted signal"}


# --------------------------------------------------------------------------- #
# MiniMax backend (OpenAI-compatible chat completions over stdlib HTTP)
# --------------------------------------------------------------------------- #
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> dict:
    """Pull the {...} object out of a model reply. Strips reasoning models'
    <think>...</think> blocks (and any unclosed leading one) and ```json fences
    first, so braces inside the reasoning don't confuse the parse."""
    text = _THINK.sub("", text)
    # drop an unclosed leading <think> ... (no closing tag, budget ran out)
    if "<think>" in text.lower():
        text = re.sub(r"(?is)<think>.*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def _minimax(contents: str, model: str) -> dict:
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY not set")

    url = f"{MINIMAX_BASE_URL}/chat/completions"  # OpenAI-compatible endpoint
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "Current screen of the session:\n\n```\n"
                                        + contents[-4000:] + "\n```\n\nClassify it."},
        ],
        # M2.7 is a reasoning model — it spends tokens on <think> before the
        # answer, so give generous headroom or the JSON gets truncated.
        "max_tokens": 4000,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())

    # MiniMax reports API-level failures in base_resp even on HTTP 200.
    base = body.get("base_resp") or {}
    if base.get("status_code", 0) not in (0, None):
        raise RuntimeError(f"minimax base_resp {base.get('status_code')}: {base.get('status_msg')}")

    content = body["choices"][0]["message"]["content"]
    data = _extract_json(content)
    action = data.get("action", "").lower()
    if action not in ("continue", "wait", "skip"):
        raise ValueError(f"unexpected action {action!r}")
    return {"action": action, "reason": data.get("reason", "")}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def judge(contents: str, model: str | None = None) -> dict:
    """Return {action, reason, backend}. Never raises — falls back to heuristic."""
    model = model or DEFAULT_MODEL
    try:
        result = _minimax(contents, model)
        result["backend"] = f"minimax:{model}"
        return result
    except Exception as e:  # missing key, network, HTTP, parse, etc.
        r = heuristic(contents)
        detail = "no MINIMAX_API_KEY" if "MINIMAX_API_KEY" in str(e) else type(e).__name__
        r["backend"] = f"heuristic ({detail})"
        return r


def health() -> dict:
    """Report whether the MiniMax backend is usable, without spending tokens."""
    has_key = bool(os.environ.get("MINIMAX_API_KEY"))
    return {
        "provider": "minimax",
        "model": DEFAULT_MODEL,
        "base_url": MINIMAX_BASE_URL,
        "credentials": has_key,
        "backend": "minimax" if has_key else "heuristic (no MINIMAX_API_KEY)",
    }


if __name__ == "__main__":
    import sys
    data = sys.stdin.read()
    print(json.dumps(judge(data), indent=2))
