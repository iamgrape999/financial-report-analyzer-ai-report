#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.
Auto-selects the first available API key in priority order:

  1. GEMINI_API_KEY   → auto-selects tier based on monthly spend (see below)
  2. ANTHROPIC_API_KEY → claude-haiku-4-5
  3. OPENAI_API_KEY   → gpt-4o-mini

Gemini cost-cap tiers (monthly accumulated spend tracked in
~/.codex_review_usage.json, resets each calendar month):

  < $15  → gemini-2.5-flash       ($0.30 / $2.50 per M in/out tokens)
  $15–$20 → gemini-2.0-flash      ($0.10 / $0.40 per M in/out tokens)
  ≥ $20  → gemini-2.0-flash-lite  ($0.075/ $0.30 per M in/out tokens)

Monthly spend is shown in every annotation so you can track it.

Optional env vars:
  CODEX_REVIEW_MODEL        — override model (disables cost-tier switching)
  CODEX_REVIEW_MAX_LINES    — skip files longer than this (default: 300)
  CODEX_REVIEW_USAGE_FILE   — path to usage JSON (default: ~/.codex_review_usage.json)

Exit code: always 0 — never blocks Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Provider auto-detection ───────────────────────────────────────────────────

GEMINI_KEY     = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY", "")
MODEL_OVERRIDE = os.getenv("CODEX_REVIEW_MODEL", "")
MAX_LINES      = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

if GEMINI_KEY:
    _PROVIDER = "gemini"
    _KEY      = GEMINI_KEY
elif ANTHROPIC_KEY:
    _PROVIDER = "anthropic"
    _KEY      = ANTHROPIC_KEY
elif OPENAI_KEY:
    _PROVIDER = "openai"
    _KEY      = OPENAI_KEY
else:
    sys.exit(0)   # no key configured → silent no-op

# ── Gemini cost-cap tier system ───────────────────────────────────────────────
# (spend_ceiling_usd, model_id, input_$/M, output_$/M)
# First entry whose ceiling exceeds current month's spend wins.

_GEMINI_TIERS: list[tuple[float, str, float, float]] = [
    (15.0, "gemini-2.5-flash",      0.30, 2.50),
    (20.0, "gemini-2.0-flash",      0.10, 0.40),
    (1e9,  "gemini-2.0-flash-lite", 0.075, 0.30),
]

_USAGE_PATH = Path(
    os.getenv("CODEX_REVIEW_USAGE_FILE",
              str(Path.home() / ".codex_review_usage.json"))
)


def _load_usage() -> dict:
    try:
        data = json.loads(_USAGE_PATH.read_text())
        if data.get("month") == datetime.now().strftime("%Y-%m"):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"month": datetime.now().strftime("%Y-%m"),
            "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}


def _save_usage(usage: dict) -> None:
    try:
        _USAGE_PATH.write_text(json.dumps(usage, indent=2))
    except OSError:
        pass


def _tier_for_cost(cost: float) -> tuple[str, float, float]:
    for ceiling, model, inp, out in _GEMINI_TIERS:
        if cost < ceiling:
            return model, inp, out
    return _GEMINI_TIERS[-1][1], _GEMINI_TIERS[-1][2], _GEMINI_TIERS[-1][3]


# Resolve model and per-token pricing
_INPUT_PER_M = _OUTPUT_PER_M = 0.0
_usage: dict = {}

if _PROVIDER == "gemini":
    _usage = _load_usage()
    if MODEL_OVERRIDE:
        _MODEL = MODEL_OVERRIDE
        matched = next((t for t in _GEMINI_TIERS if t[1] == MODEL_OVERRIDE), None)
        _INPUT_PER_M, _OUTPUT_PER_M = (matched[2], matched[3]) if matched else (0.30, 2.50)
    else:
        _MODEL, _INPUT_PER_M, _OUTPUT_PER_M = _tier_for_cost(_usage["cost_usd"])
elif _PROVIDER == "anthropic":
    _MODEL = MODEL_OVERRIDE or "claude-haiku-4-5-20251001"
else:
    _MODEL = MODEL_OVERRIDE or "gpt-4o-mini"

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

# ── Update Gemini cost tracking ───────────────────────────────────────────────

if _PROVIDER == "gemini":
    meta    = raw.get("usageMetadata", {})
    in_tok  = int(meta.get("promptTokenCount", 0))
    out_tok = int(meta.get("candidatesTokenCount", 0))
    cost    = (in_tok * _INPUT_PER_M + out_tok * _OUTPUT_PER_M) / 1_000_000
    _usage["input_tokens"]  = _usage.get("input_tokens", 0) + in_tok
    _usage["output_tokens"] = _usage.get("output_tokens", 0) + out_tok
    _usage["cost_usd"]      = round(_usage.get("cost_usd", 0.0) + cost, 6)
    _save_usage(_usage)

# ── Print annotation ──────────────────────────────────────────────────────────

icon = "✓" if review.startswith("✓") else "🔍"

if _PROVIDER == "gemini":
    provider_tag  = f"Gemini/{_MODEL}"
    monthly_note  = f"  [month: ${_usage['cost_usd']:.3f}]"
else:
    provider_tag  = {"anthropic": "Claude", "openai": "GPT-4o-mini"}[_PROVIDER]
    monthly_note  = ""

print(
    f"\n{icon} [{provider_tag}] review — {os.path.basename(file_path)}{monthly_note}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
