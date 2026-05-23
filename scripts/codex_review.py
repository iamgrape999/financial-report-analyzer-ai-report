#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.
Auto-selects the first available API key in priority order:

  1. GEMINI_API_KEY   → gemini-2.0-flash-lite  (already used by this project)
  2. ANTHROPIC_API_KEY → claude-haiku-4-5       (cheapest, highest quality)
  3. OPENAI_API_KEY   → gpt-4o-mini             (requires separate API account)

No extra accounts needed if GEMINI_API_KEY or ANTHROPIC_API_KEY is already set.

Optional env vars:
  CODEX_REVIEW_MODEL     — override model for whichever provider is selected
  CODEX_REVIEW_MAX_LINES — skip files longer than this (default: 300)

Exit code: always 0 — never blocks Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ── Provider auto-detection ───────────────────────────────────────────────────

GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
MODEL_OVERRIDE = os.getenv("CODEX_REVIEW_MODEL", "")
MAX_LINES     = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

if GEMINI_KEY:
    _PROVIDER = "gemini"
    _KEY      = GEMINI_KEY
    _MODEL    = MODEL_OVERRIDE or "gemini-2.0-flash-lite"
elif ANTHROPIC_KEY:
    _PROVIDER = "anthropic"
    _KEY      = ANTHROPIC_KEY
    _MODEL    = MODEL_OVERRIDE or "claude-haiku-4-5-20251001"
elif OPENAI_KEY:
    _PROVIDER = "openai"
    _KEY      = OPENAI_KEY
    _MODEL    = MODEL_OVERRIDE or "gpt-4o-mini"
else:
    sys.exit(0)   # no key configured → silent no-op

# ── File filtering ────────────────────────────────────────────────────────────

file_path = os.getenv("CLAUDE_TOOL_INPUT_FILE_PATH", "")

if not file_path.endswith(".py"):
    sys.exit(0)
if not ("/credit_report/" in file_path or file_path.endswith("main.py")):
    sys.exit(0)
if any(seg in file_path for seg in ["/tests/", "_test.py", "test_", "conftest"]):
    sys.exit(0)

try:
    with open(file_path) as fh:
        lines = fh.readlines()
except (OSError, FileNotFoundError):
    sys.exit(0)

if len(lines) > MAX_LINES:
    print(
        f"[codex-review] ⏭  {os.path.basename(file_path)} "
        f"({len(lines)} lines > {MAX_LINES} limit) — skipped",
        flush=True,
    )
    sys.exit(0)

content = "".join(lines)

# ── Prompt ────────────────────────────────────────────────────────────────────

PROMPT = (
    "You are a senior Python security engineer reviewing a FastAPI/SQLAlchemy "
    "async codebase.  Review the file below and report ONLY:\n"
    "  • Security vulnerabilities (SQL injection, IDOR, privilege escalation)\n"
    "  • Critical async bugs (missing await, session misuse, race conditions)\n"
    "  • Data-integrity risks (missing flush, wrong transaction boundary)\n\n"
    "Rules:\n"
    "  – Max 3 bullet points, each ≤ 20 words.\n"
    "  – If nothing serious, reply with exactly:  ✓ No critical issues.\n"
    "  – Do NOT praise or explain what the code does.\n\n"
    f"File: {os.path.basename(file_path)}\n"
    f"```python\n{content}\n```"
)

# ── Provider-specific request builders ───────────────────────────────────────

def _gemini_request() -> urllib.request.Request:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{_MODEL}:generateContent?key={_KEY}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.1},
    }).encode()
    return urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")


def _anthropic_request() -> urllib.request.Request:
    body = json.dumps({
        "model": _MODEL,
        "max_tokens": 200,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": PROMPT}],
    }).encode()
    return urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": _KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )


def _openai_request() -> urllib.request.Request:
    body = json.dumps({
        "model": _MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 200,
        "temperature": 0.1,
    }).encode()
    return urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )


def _parse_response(data: dict) -> str:
    if _PROVIDER == "gemini":
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    if _PROVIDER == "anthropic":
        return data["content"][0]["text"].strip()
    return data["choices"][0]["message"]["content"].strip()   # openai


# ── Call API ──────────────────────────────────────────────────────────────────

_builders = {"gemini": _gemini_request, "anthropic": _anthropic_request,
             "openai": _openai_request}

try:
    req = _builders[_PROVIDER]()
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read())
    review = _parse_response(raw)
except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)   # any failure → silent, non-blocking

# ── Print annotation ──────────────────────────────────────────────────────────

icon = "✓" if review.startswith("✓") else "🔍"
provider_tag = {"gemini": "Gemini", "anthropic": "Claude", "openai": "GPT-4o-mini"}[_PROVIDER]
print(
    f"\n{icon} [{provider_tag}] review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
