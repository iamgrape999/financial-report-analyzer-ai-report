#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.

Set both vars in Render environment to enable:
  GEMINI_REVIEWER_API_KEY  — your Gemini API key (leave blank → no reviews)
  GEMINI_REVIEWER_MODEL    — model to use (default: gemini-2.5-pro)

If GEMINI_REVIEWER_API_KEY is not set, the hook exits silently — no review,
no error, no fallback.

Exit code: always 0 — never blocks Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MAX_LINES = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

# ── Provider ──────────────────────────────────────────────────────────────────

_GEMINI_KEY   = os.getenv("GEMINI_REVIEWER_API_KEY", "")
_GEMINI_MODEL = os.getenv("GEMINI_REVIEWER_MODEL") or "gemini-2.5-pro"

if not _GEMINI_KEY:
    sys.exit(0)  # no key → silent no-op

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

try:
    model_id = _GEMINI_MODEL.split("/")[-1]
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent?key={_GEMINI_KEY}"
    )
    body = json.dumps({
        "contents":         [{"parts": [{"text": PROMPT}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.1},
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read())
    review = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)  # any failure → silent, non-blocking

# ── Print annotation ──────────────────────────────────────────────────────────

icon        = "✓" if review.startswith("✓") else "🔍"
model_short = _GEMINI_MODEL.split("/")[-1]

print(
    f"\n{icon} [Gemini/{model_short}] review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
