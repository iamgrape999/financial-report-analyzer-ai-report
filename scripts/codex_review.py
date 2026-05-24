#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.
Provider priority (first key found wins):

  1. GEMINI_API_KEY      → gemini-2.5-pro    ✅ works from iOS/web (Anthropic sandbox)
  2. OPENROUTER_API_KEY  → poolside/laguna-m.1:free   (local CLI only)
  3. CEREBRAS_API_KEY    → llama3.1-70b                (local CLI only)
  4. GROQ_API_KEY        → llama-3.3-70b-versatile     (local CLI only)

Gemini 2.5 Pro is stronger than Claude Sonnet 4.6 — ideal as a reviewer
when Sonnet 4.6 is the daily coder.

Override model for whichever provider wins:
  CODEX_REVIEW_MODEL=<model-id>

Skip files longer than:
  CODEX_REVIEW_MAX_LINES=300  (default)

Exit code: always 0 — never blocks Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MAX_LINES = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

# ── Provider auto-detection ───────────────────────────────────────────────────

_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
_GEMINI_MODEL = os.getenv("CODEX_REVIEW_MODEL") or "gemini-2.5-pro"

_OAI_CANDIDATES = [
    ("OpenRouter", os.getenv("OPENROUTER_API_KEY", ""),
     "https://openrouter.ai/api/v1/chat/completions",
     os.getenv("OPENROUTER_MODEL") or "poolside/laguna-m.1:free"),
    ("Cerebras",   os.getenv("CEREBRAS_API_KEY", ""),
     "https://api.cerebras.ai/v1/chat/completions",
     os.getenv("CEREBRAS_MODEL") or "llama3.1-70b"),
    ("Groq",       os.getenv("GROQ_API_KEY", ""),
     "https://api.groq.com/openai/v1/chat/completions",
     os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile"),
]

_OAI_MATCH = next((c for c in _OAI_CANDIDATES if c[1]), None)

if not _GEMINI_KEY and not _OAI_MATCH:
    sys.exit(0)  # no key configured → silent no-op

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
    if _GEMINI_KEY:
        # Gemini uses a different API format from OpenAI-compatible providers
        _PROVIDER = "Gemini"
        _MODEL    = _GEMINI_MODEL
        _model_id = _MODEL.split("/")[-1]
        url  = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{_model_id}:generateContent?key={_GEMINI_KEY}"
        )
        body = json.dumps({
            "contents":       [{"parts": [{"text": PROMPT}]}],
            "generationConfig": {"maxOutputTokens": 200, "temperature": 0.1},
        }).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read())
        review = raw["candidates"][0]["content"]["parts"][0]["text"].strip()

    else:
        # OpenAI-compatible providers (OpenRouter / Cerebras / Groq)
        _PROVIDER, _KEY, _URL, _DEFAULT = _OAI_MATCH
        _MODEL = os.getenv("CODEX_REVIEW_MODEL") or _DEFAULT
        headers = {"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"}
        if _PROVIDER == "OpenRouter":
            headers["HTTP-Referer"] = "https://github.com/iamgrape999/financial-report-analyzer-ai-report"
            headers["X-Title"]      = "CathyChang AI Code Review"
        body = json.dumps({
            "model":       _MODEL,
            "messages":    [{"role": "user", "content": PROMPT}],
            "max_tokens":  200,
            "temperature": 0.1,
        }).encode()
        req = urllib.request.Request(_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read())
        review = raw["choices"][0]["message"]["content"].strip()

except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)  # any failure → silent, non-blocking

# ── Print annotation ──────────────────────────────────────────────────────────

icon        = "✓" if review.startswith("✓") else "🔍"
model_short = _MODEL.split("/")[-1].replace(":free", "")

print(
    f"\n{icon} [{_PROVIDER}/{model_short}] review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
