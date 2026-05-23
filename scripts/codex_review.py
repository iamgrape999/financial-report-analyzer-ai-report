#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.
Auto-selects the first available key in priority order — all free/cheap:

  1. OPENROUTER_API_KEY  → poolside/laguna-m.1:free   (coding agent, $0)
  2. CEREBRAS_API_KEY    → llama3.1-70b                (ultra-fast inference)
  3. GROQ_API_KEY        → llama-3.3-70b-versatile     (fast inference)

All three already exist in this project's .env — no new accounts needed.
All three use the same OpenAI-compatible API format.

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

# ── Provider auto-detection (first key found wins) ────────────────────────────

_CANDIDATES = [
    # (provider_label, api_key, api_url, default_model)
    (
        "OpenRouter",
        os.getenv("OPENROUTER_API_KEY", ""),
        "https://openrouter.ai/api/v1/chat/completions",
        os.getenv("OPENROUTER_MODEL") or "poolside/laguna-m.1:free",
    ),
    (
        "Cerebras",
        os.getenv("CEREBRAS_API_KEY", ""),
        "https://api.cerebras.ai/v1/chat/completions",
        os.getenv("CEREBRAS_MODEL") or "llama3.1-70b",
    ),
    (
        "Groq",
        os.getenv("GROQ_API_KEY", ""),
        "https://api.groq.com/openai/v1/chat/completions",
        os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile",
    ),
]

_match = next((c for c in _CANDIDATES if c[1]), None)
if not _match:
    sys.exit(0)   # no key configured → silent no-op

_PROVIDER, _KEY, _URL, _MODEL = _match
_MODEL = os.getenv("CODEX_REVIEW_MODEL") or _MODEL   # explicit override wins

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

# ── API call (OpenAI-compatible — same format for all three providers) ─────────

headers = {
    "Authorization": f"Bearer {_KEY}",
    "Content-Type":  "application/json",
}
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

try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read())
    review = raw["choices"][0]["message"]["content"].strip()
except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)   # any failure → silent, non-blocking

# ── Print annotation ──────────────────────────────────────────────────────────

icon        = "✓" if review.startswith("✓") else "🔍"
model_short = _MODEL.split("/")[-1].replace(":free", "")

print(
    f"\n{icon} [{_PROVIDER}/{model_short}] review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
