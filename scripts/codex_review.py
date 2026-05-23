#!/usr/bin/env python3
"""AI code-review hook for Claude Code.

PostToolUse hook triggered on Edit/Write to production Python files.
Uses OPENROUTER_API_KEY (already configured in this project) with
free-tier models — zero cost per review.

Free model options (set CODEX_REVIEW_MODEL to switch):
  poolside/laguna-m.1:free          ← best for code (coding agent, #15 Programming)
  nvidia/nemotron-super-49b-v1:free ← strong general reasoning
  openai/gpt-oss-120b:free          ← OpenAI open-weight 120B
  deepseek/deepseek-chat:free       ← reliable fallback

Default priority:
  1. CODEX_REVIEW_MODEL env var (explicit override)
  2. OPENROUTER_MODEL env var (reuse project's existing setting)
  3. poolside/laguna-m.1:free (best for code review)

Optional env vars:
  CODEX_REVIEW_MAX_LINES  — skip files longer than this (default: 300)

Exit code: always 0 — never blocks Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ── API key ───────────────────────────────────────────────────────────────────

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
MAX_LINES      = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

if not OPENROUTER_KEY:
    sys.exit(0)   # no key configured → silent no-op

# ── Model selection ───────────────────────────────────────────────────────────

_MODEL = (
    os.getenv("CODEX_REVIEW_MODEL")          # 1. explicit override
    or os.getenv("OPENROUTER_MODEL")          # 2. project's existing setting
    or "poolside/laguna-m.1:free"             # 3. best free coding model
)

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

# ── API call (OpenRouter — OpenAI-compatible format) ──────────────────────────

body = json.dumps({
    "model": _MODEL,
    "messages": [{"role": "user", "content": PROMPT}],
    "max_tokens": 200,
    "temperature": 0.1,
}).encode()

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=body,
    headers={
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/iamgrape999/financial-report-analyzer-ai-report",
        "X-Title":       "CathyChang AI Code Review",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read())
    review = raw["choices"][0]["message"]["content"].strip()
except (urllib.error.URLError, KeyError, IndexError,
        json.JSONDecodeError, TimeoutError):
    sys.exit(0)   # any failure → silent, non-blocking

# ── Print annotation ──────────────────────────────────────────────────────────

icon         = "✓" if review.startswith("✓") else "🔍"
model_short  = _MODEL.split("/")[-1].replace(":free", "")   # e.g. "laguna-m.1"

print(
    f"\n{icon} [OpenRouter/{model_short}] review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
