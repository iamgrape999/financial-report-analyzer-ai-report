#!/usr/bin/env python3
"""Codex (OpenAI) review hook for Claude Code.

PostToolUse hook on Edit/Write tools.  For each production Python file
touched by Claude Code, sends the file content to GPT-4o-mini and prints
a focused bug/security review as a non-blocking annotation.

Required env var : OPENAI_API_KEY
Optional env vars:
  CODEX_REVIEW_MODEL     — OpenAI model id  (default: gpt-4o-mini)
  CODEX_REVIEW_MAX_LINES — skip files with more lines than this  (default: 300)

Exit codes:
  0 always — hook never blocks the edit.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY  = os.getenv("OPENAI_API_KEY", "")
MODEL    = os.getenv("CODEX_REVIEW_MODEL", "gpt-4o-mini")
MAX_LINES = int(os.getenv("CODEX_REVIEW_MAX_LINES", "300"))

# Skip silently when no API key is configured
if not API_KEY:
    sys.exit(0)

# ── Which file was edited? ────────────────────────────────────────────────────

file_path = os.getenv("CLAUDE_TOOL_INPUT_FILE_PATH", "")

# Only review production Python inside credit_report/
if not file_path.endswith(".py"):
    sys.exit(0)
if not (
    "/credit_report/" in file_path
    or file_path.endswith("main.py")
):
    sys.exit(0)

# Never review test files — they get their own validation from pytest
if any(seg in file_path for seg in ["/tests/", "_test.py", "test_", "conftest"]):
    sys.exit(0)

# ── Read file content ─────────────────────────────────────────────────────────

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

# ── Build prompt ──────────────────────────────────────────────────────────────

prompt = (
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

# ── Call OpenAI ───────────────────────────────────────────────────────────────

payload = json.dumps({
    "model": MODEL,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 180,
    "temperature": 0.1,
}).encode()

req = urllib.request.Request(
    "https://api.openai.com/v1/chat/completions",
    data=payload,
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read())
    review = data["choices"][0]["message"]["content"].strip()
except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError):
    # Any API failure is silently ignored — never block the edit
    sys.exit(0)

# ── Print review annotation ───────────────────────────────────────────────────

icon = "✓" if review.startswith("✓") else "🔍"
print(
    f"\n{icon} Codex review — {os.path.basename(file_path)}:\n"
    f"{review}\n",
    flush=True,
)
sys.exit(0)
