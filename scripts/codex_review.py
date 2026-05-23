#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.
Uses GEMINI_API_KEY (already required by this project — no extra account needed).

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

# ── API key ───────────────────────────────────────────────────────────────────

GEMINI_KEY     = os.getenv("GEMINI_API_KEY", "")
MODEL_OVERRIDE = os.getenv("CODEX_REVIEW_MODEL", "")
MAX_LINES      = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

if not GEMINI_KEY:
    sys.exit(0)   # no key configured → silent no-op

# ── Cost-cap tier system ──────────────────────────────────────────────────────
# (spend_ceiling_usd, model_id, input_$/M, output_$/M)
# First entry whose ceiling exceeds current month's spend wins.

_TIERS: list[tuple[float, str, float, float]] = [
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
    for ceiling, model, inp, out in _TIERS:
        if cost < ceiling:
            return model, inp, out
    return _TIERS[-1][1], _TIERS[-1][2], _TIERS[-1][3]


# Resolve model and per-token pricing
_usage = _load_usage()

if MODEL_OVERRIDE:
    _MODEL = MODEL_OVERRIDE
    matched = next((t for t in _TIERS if t[1] == MODEL_OVERRIDE), None)
    _INPUT_PER_M, _OUTPUT_PER_M = (matched[2], matched[3]) if matched else (0.30, 2.50)
else:
    _MODEL, _INPUT_PER_M, _OUTPUT_PER_M = _tier_for_cost(_usage["cost_usd"])

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

# ── API call ──────────────────────────────────────────────────────────────────

url = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_MODEL}:generateContent?key={GEMINI_KEY}"
)
body = json.dumps({
    "contents": [{"parts": [{"text": PROMPT}]}],
    "generationConfig": {"maxOutputTokens": 200, "temperature": 0.1},
}).encode()
req = urllib.request.Request(url, data=body,
                             headers={"Content-Type": "application/json"},
                             method="POST")

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read())
    review = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)   # any failure → silent, non-blocking

# ── Update cost tracking ──────────────────────────────────────────────────────

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
print(
    f"\n{icon} [Gemini/{_MODEL}] review — {os.path.basename(file_path)}"
    f"  [month: ${_usage['cost_usd']:.3f}]:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
