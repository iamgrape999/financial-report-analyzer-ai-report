#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.

Set both vars in Render environment to enable:
  GEMINI_REVIEWER_API_KEY  — your Gemini API key (leave blank → no reviews)
  GEMINI_REVIEWER_MODEL    — model to use (default: gemini-2.5-flash)

If GEMINI_REVIEWER_API_KEY is not set, the hook exits silently — no review,
no error, no fallback.

Exit code: always 0 — never blocks Claude Code.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

MAX_LINES = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

# ── Provider ──────────────────────────────────────────────────────────────────

_GEMINI_KEY   = os.getenv("GEMINI_REVIEWER_API_KEY", "")
_GEMINI_MODEL = os.getenv("GEMINI_REVIEWER_MODEL") or "gemini-2.5-flash"

# M4: validate key format — Gemini API keys are "AIza..." and ≥ 35 chars
if not _GEMINI_KEY or not _GEMINI_KEY.startswith("AIza") or len(_GEMINI_KEY) < 35:
    sys.exit(0)  # no key / invalid format → silent no-op

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

# ── M6: Redact secrets before sending to external API ─────────────────────────

_SECRET_PATTERNS = [
    (re.compile(r'AIza[0-9A-Za-z_\-]{35,}'), "AIza[REDACTED]"),
    (re.compile(r'sk-[A-Za-z0-9]{20,}'),      "sk-[REDACTED]"),
    (re.compile(r'Bearer\s+\S{20,}'),          "Bearer [REDACTED]"),
]

def _redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

content = _redact(content)

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
    # H3: API key in header, not URL query string
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent"
    )
    body = json.dumps({
        "contents":         [{"parts": [{"text": PROMPT}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.1},
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": _GEMINI_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read())
    review = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
except urllib.error.HTTPError as exc:
    # H2: surface HTTP error codes to stderr for debugging; never blocks
    print(
        f"[codex-review] HTTP {exc.code} from Gemini API "
        f"({os.path.basename(file_path)}) — review skipped",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(0)
except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)  # any other failure → silent, non-blocking

# ── Print annotation ──────────────────────────────────────────────────────────

# M3: broader clean-review detection — match any leading ✓ / OK / LGTM / no issues
_CLEAN_RE = re.compile(r"^(✓|✅|ok|lgtm|no\s+critical)", re.IGNORECASE)
icon        = "✓" if _CLEAN_RE.match(review) else "🔍"
model_short = _GEMINI_MODEL.split("/")[-1]

print(
    f"\n{icon} [Gemini/{model_short}] review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
