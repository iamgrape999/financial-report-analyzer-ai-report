#!/usr/bin/env python3
"""Verification & live test for the OpenRouter code-review hook.

Usage
-----
  # Mock only (no API cost — always works):
  python3 scripts/test_codex_review.py

  # Live test (uses free model — also no cost):
  OPENROUTER_API_KEY=sk-or-... python3 scripts/test_codex_review.py

Exit codes:  0 = all passed,  1 = failures
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT    = os.path.join(ROOT, "scripts", "codex_review.py")
PROD_FILE = os.path.join(ROOT, "credit_report", "audit", "events.py")
BIG_FILE  = os.path.join(ROOT, "credit_report", "api", "reports.py")
TEST_FILE = os.path.join(ROOT, "tests", "test_gap_coverage.py")
HTML_FILE = os.path.join(ROOT, "static", "index.html")

LIVE_KEY = os.getenv("OPENROUTER_API_KEY", "")
LIVE     = bool(LIVE_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# Mock HTTP server  (OpenAI-compatible — same format OpenRouter uses)
# ─────────────────────────────────────────────────────────────────────────────

_QUEUE: list[str] = []

_CLEAN = json.dumps({"choices": [{"message": {"content": "✓ No critical issues."}}]})
_WARN  = json.dumps({"choices": [{"message": {"content":
    "• `mark_resolved` does not verify report ownership — IDOR possible.\n"
    "• Missing `await db.flush()` before conflict state re-query.\n"
    "• No approved-report immutability guard on conflict endpoints."}}]})


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = (_QUEUE.pop(0) if _QUEUE else _CLEAN)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


_srv  = http.server.HTTPServer(("127.0.0.1", 0), _MockHandler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def _patched() -> str:
    """Return path to a temp copy whose OpenRouter URL points at mock server."""
    with open(SCRIPT) as f:
        src = f.read()
    src = src.replace("https://openrouter.ai/api/v1/chat/completions",
                      f"http://127.0.0.1:{_port}/v1/chat/completions")
    tf = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False)
    tf.write(src); tf.flush()
    return tf.name


# ─────────────────────────────────────────────────────────────────────────────
# Assertion helpers
# ─────────────────────────────────────────────────────────────────────────────

RESULTS: list[tuple[bool, str, str]] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    icon = "✅" if ok else "❌"
    RESULTS.append((ok, desc, detail))
    print(f"  {icon}  {desc}" + (f"  →  {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")


def _run(script: str, *, file_path: str = "",
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    if file_path:
        env["CLAUDE_TOOL_INPUT_FILE_PATH"] = file_path
    return subprocess.run([sys.executable, script], env=env,
                          capture_output=True, text=True)


# ─────────────────────────────────────────────────────────────────────────────
# Test groups
# ─────────────────────────────────────────────────────────────────────────────

def test_exclusions() -> None:
    section("A — Exclusion / no-op branches  (zero API calls)")

    print("\n  [1] No API key set")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env={"OPENROUTER_API_KEY": ""})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [2] No file path set")
    p = _run(SCRIPT, extra_env={"OPENROUTER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [3] HTML file (.html) excluded")
    p = _run(SCRIPT, file_path=HTML_FILE, extra_env={"OPENROUTER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [4] Test file excluded")
    p = _run(SCRIPT, file_path=TEST_FILE, extra_env={"OPENROUTER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [5] File exceeds MAX_LINES (limit=10)")
    p = _run(SCRIPT, file_path=BIG_FILE,
             extra_env={"OPENROUTER_API_KEY": "fake", "CODEX_REVIEW_MAX_LINES": "10"})
    check("exit 0", p.returncode == 0)
    check("prints skip notice", "skipped" in p.stdout.lower(), p.stdout[:100])

    print("\n  [6] Nonexistent file → silent")
    p = _run(SCRIPT, file_path=os.path.join(ROOT, "credit_report", "ghost.py"),
             extra_env={"OPENROUTER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")


def test_error_resilience() -> None:
    section("B — Error resilience  (API failures must never crash)")

    print("\n  [7] Unreachable API URL → silent")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env={"OPENROUTER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no Traceback", "Traceback" not in p.stdout + p.stderr)

    print("\n  [8] Malformed JSON response → silent")
    patched = _patched()
    try:
        _QUEUE.clear(); _QUEUE.append(b"not-json{")  # type: ignore[arg-type]
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={"OPENROUTER_API_KEY": "fake"})
        check("exit 0", p.returncode == 0)
        check("no Traceback", "Traceback" not in p.stdout + p.stderr)
    finally:
        os.unlink(patched)


def test_happy_paths() -> None:
    section("C — Happy paths via mock server")
    patched = _patched()
    env     = {"OPENROUTER_API_KEY": "fake-key",
               "CODEX_REVIEW_MODEL": "poolside/laguna-m.1:free"}
    try:
        print("\n  [clean] returns ✓ No critical issues")
        _QUEUE.clear(); _QUEUE.append(_CLEAN)
        p = _run(patched, file_path=PROD_FILE, extra_env=env)
        check("exit 0",           p.returncode == 0)
        check("prints ✓",         "✓" in p.stdout,         p.stdout[:200])
        check("shows filename",   "events.py" in p.stdout)
        check("shows OpenRouter", "OpenRouter/" in p.stdout)
        check("shows model",      "laguna-m.1" in p.stdout)
        check("no 🔍",            "🔍" not in p.stdout)
        print(f"       Output: {p.stdout.strip()}")

        print("\n  [warn] returns issues found")
        _QUEUE.clear(); _QUEUE.append(_WARN)
        p = _run(patched, file_path=PROD_FILE, extra_env=env)
        check("exit 0",           p.returncode == 0)
        check("prints 🔍",        "🔍" in p.stdout,         p.stdout[:200])
        check("shows filename",   "events.py" in p.stdout)
        check("shows OpenRouter", "OpenRouter/" in p.stdout)
        check("contains bullets", "•" in p.stdout)
        print(f"       Output:\n{p.stdout.strip()}")

        print("\n  [model fallback] uses OPENROUTER_MODEL when CODEX_REVIEW_MODEL unset")
        _QUEUE.clear(); _QUEUE.append(_CLEAN)
        p = _run(patched, file_path=PROD_FILE, extra_env={
            "OPENROUTER_API_KEY": "fake-key",
            "OPENROUTER_MODEL":   "nvidia/nemotron-super-49b-v1:free",
            "CODEX_REVIEW_MODEL": "",
        })
        check("exit 0",               p.returncode == 0)
        check("uses nemotron model",  "nemotron" in p.stdout, p.stdout[:200])
        print(f"       Output: {p.stdout.strip()}")
    finally:
        os.unlink(patched)


def test_live() -> None:
    section("D — Live call  (real OpenRouter API — free model, $0 cost)")

    if not LIVE:
        print(
            "\n  ⚠️  OPENROUTER_API_KEY not set — live section skipped.\n"
            "\n  To run (costs nothing — free model):\n"
            "    OPENROUTER_API_KEY=sk-or-... python3 scripts/test_codex_review.py\n"
        )
        return

    n_lines = sum(1 for _ in open(PROD_FILE))
    print(f"\n  Key    : {LIVE_KEY[:12]}...{LIVE_KEY[-4:]}")
    print(f"  Model  : {os.getenv('CODEX_REVIEW_MODEL') or os.getenv('OPENROUTER_MODEL') or 'poolside/laguna-m.1:free'}")
    print(f"  Target : credit_report/audit/events.py  ({n_lines} lines)")
    print(f"  Cost   : $0.00  (free tier model)")

    t0 = time.monotonic()
    p  = _run(SCRIPT, file_path=PROD_FILE)
    elapsed = time.monotonic() - t0

    print(f"\n  Response ({elapsed:.1f}s):\n")
    for line in p.stdout.strip().splitlines():
        print(f"  {line}")

    check("exit 0",              p.returncode == 0, p.stderr[:300] if p.returncode else "")
    check("no Traceback",        "Traceback" not in p.stdout + p.stderr)
    check("non-empty output",    p.stdout.strip() != "")
    check("annotation icon",     "✓" in p.stdout or "🔍" in p.stdout)
    check("shows filename",      "events.py" in p.stdout)
    check("shows OpenRouter tag","OpenRouter/" in p.stdout)
    check("finished < 30s",      elapsed < 30, f"took {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═"*60)
    print("  OpenRouter Code-Review Hook — Verification Suite")
    print("  Free models · $0 per review · no cost cap needed")
    if LIVE:
        print(f"  Live key : {LIVE_KEY[:12]}...{LIVE_KEY[-4:]}")
    else:
        print("  Mode     : mock only  (OPENROUTER_API_KEY not set)")
    print("═"*60)

    test_exclusions()
    test_error_resilience()
    test_happy_paths()
    test_live()

    _srv.shutdown()

    passed = sum(1 for ok, *_ in RESULTS if ok)
    failed = sum(1 for ok, *_ in RESULTS if not ok)

    print("\n" + "═"*60)
    print(f"  {'✅' if failed == 0 else '❌'}  {passed} passed, {failed} failed"
          f"  ({len(RESULTS)} assertions)")
    if failed:
        print("\n  Failed:")
        for ok, desc, detail in RESULTS:
            if not ok:
                print(f"    ❌  {desc}  {detail}")
    print("═"*60 + "\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
