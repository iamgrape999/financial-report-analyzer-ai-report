#!/usr/bin/env python3
"""Verification & live test for the Gemini code-review hook.

Usage
-----
  # Mock only (no API cost — always works):
  python3 scripts/test_codex_review.py

  # Live test:
  GEMINI_REVIEWER_API_KEY=AIza...  python3 scripts/test_codex_review.py

If GEMINI_REVIEWER_API_KEY is not set, the hook exits silently — no fallback.

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

LIVE_KEY = os.getenv("GEMINI_REVIEWER_API_KEY", "")
LIVE     = bool(LIVE_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# Mock HTTP server (Gemini response format)
# ─────────────────────────────────────────────────────────────────────────────

_QUEUE: list[str] = []

_CLEAN = json.dumps({
    "candidates": [{"content": {"parts": [{"text": "✓ No critical issues."}]}}]
})
_WARN = json.dumps({"candidates": [{"content": {"parts": [{"text":
    "• `mark_resolved` does not verify report ownership — IDOR possible.\n"
    "• Missing `await db.flush()` before conflict state re-query.\n"
    "• No approved-report immutability guard on conflict endpoints."
}]}}]})


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

_GEMINI_BASE = "https://generativelanguage.googleapis.com"
_MOCK_BASE   = f"http://127.0.0.1:{_port}"


def _patched() -> str:
    """Return path to a temp script with Gemini URL pointing at mock server."""
    with open(SCRIPT) as f:
        src = f.read()
    src = src.replace(_GEMINI_BASE, _MOCK_BASE)
    tf = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False)
    tf.write(src); tf.flush()
    return tf.name


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
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


_NO_KEY = {"GEMINI_REVIEWER_API_KEY": ""}

# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_exclusions() -> None:
    section("A — Exclusion / no-op branches  (zero API calls)")

    print("\n  [1] No API key → silent no-op")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env=_NO_KEY)
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [2] No file path set")
    p = _run(SCRIPT, extra_env={"GEMINI_REVIEWER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [3] HTML file excluded")
    p = _run(SCRIPT, file_path=HTML_FILE, extra_env={"GEMINI_REVIEWER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [4] Test file excluded")
    p = _run(SCRIPT, file_path=TEST_FILE, extra_env={"GEMINI_REVIEWER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [5] File exceeds MAX_LINES (limit=10)")
    p = _run(SCRIPT, file_path=BIG_FILE,
             extra_env={"GEMINI_REVIEWER_API_KEY": "fake", "CODEX_REVIEW_MAX_LINES": "10"})
    check("exit 0", p.returncode == 0)
    check("prints skip notice", "skipped" in p.stdout.lower(), p.stdout[:100])

    print("\n  [6] Nonexistent file → silent")
    p = _run(SCRIPT, file_path=os.path.join(ROOT, "credit_report", "ghost.py"),
             extra_env={"GEMINI_REVIEWER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")


def test_error_resilience() -> None:
    section("B — Error resilience  (failures must never crash)")

    print("\n  [7] Unreachable URL → silent")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env={"GEMINI_REVIEWER_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no Traceback", "Traceback" not in p.stdout + p.stderr)

    print("\n  [8] Malformed JSON response → silent")
    patched = _patched()
    try:
        _QUEUE.clear(); _QUEUE.append(b"not-json{")  # type: ignore[arg-type]
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={"GEMINI_REVIEWER_API_KEY": "fake"})
        check("exit 0", p.returncode == 0)
        check("no Traceback", "Traceback" not in p.stdout + p.stderr)
    finally:
        os.unlink(patched)


def test_happy_paths() -> None:
    section("C — Happy paths via mock server")
    patched = _patched()
    try:
        print("\n  [clean] Returns ✓ No critical issues")
        _QUEUE.clear(); _QUEUE.append(_CLEAN)
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={"GEMINI_REVIEWER_API_KEY": "fake",
                             "GEMINI_REVIEWER_MODEL":  "gemini-3.5-flash"})
        check("exit 0",         p.returncode == 0)
        check("prints ✓",       "✓" in p.stdout,           p.stdout[:200])
        check("shows filename", "events.py" in p.stdout)
        check("shows Gemini",   "Gemini/" in p.stdout)
        check("shows model",    "gemini-3.5-flash" in p.stdout)
        check("no 🔍",          "🔍" not in p.stdout)
        print(f"       Output: {p.stdout.strip()}")

        print("\n  [warn] Returns issues found")
        _QUEUE.clear(); _QUEUE.append(_WARN)
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={"GEMINI_REVIEWER_API_KEY": "fake",
                             "GEMINI_REVIEWER_MODEL":  "gemini-2.5-flash"})
        check("exit 0",           p.returncode == 0)
        check("prints 🔍",        "🔍" in p.stdout,          p.stdout[:200])
        check("shows filename",   "events.py" in p.stdout)
        check("shows Gemini",     "Gemini/" in p.stdout)
        check("shows model",      "gemini-2.5-flash" in p.stdout)
        check("contains bullets", "•" in p.stdout)
        print(f"       Output:\n{p.stdout.strip()}")
    finally:
        os.unlink(patched)


def test_model_override() -> None:
    section("D — GEMINI_REVIEWER_MODEL swap (all supported models)")
    patched = _patched()
    models = [
        "gemini-3.5-flash",        # default — best for coding/agentic tasks
        "gemini-3.1-pro-preview",  # SOTA reasoning
        "gemini-3-flash-preview",  # frontier + grounding
        "gemini-3.1-flash-lite",   # highest volume, lowest cost
        "gemini-2.5-pro",          # previous gen pro
        "gemini-2.5-flash",        # previous gen flash
    ]
    try:
        for model in models:
            _QUEUE.clear(); _QUEUE.append(_CLEAN)
            p = _run(patched, file_path=PROD_FILE,
                     extra_env={"GEMINI_REVIEWER_API_KEY": "fake",
                                 "GEMINI_REVIEWER_MODEL":  model})
            check(f"model={model}", model in p.stdout, p.stdout[:120])
    finally:
        os.unlink(patched)


def test_live() -> None:
    section("E — Live call  (real Gemini API)")

    if not LIVE:
        print(
            "\n  ⚠️  GEMINI_REVIEWER_API_KEY not set — live section skipped.\n"
            "  Set it in Render environment variables to enable reviews.\n"
        )
        return

    n_lines = sum(1 for _ in open(PROD_FILE))
    model   = os.getenv("GEMINI_REVIEWER_MODEL", "gemini-2.5-pro")
    print(f"\n  Key   : {LIVE_KEY[:12]}...{LIVE_KEY[-4:]}")
    print(f"  Model : {model}")
    print(f"  Target: credit_report/audit/events.py  ({n_lines} lines)")

    t0      = time.monotonic()
    p       = _run(SCRIPT, file_path=PROD_FILE)
    elapsed = time.monotonic() - t0

    print(f"\n  Response ({elapsed:.1f}s):\n")
    for line in p.stdout.strip().splitlines():
        print(f"  {line}")

    check("exit 0",           p.returncode == 0, p.stderr[:300] if p.returncode else "")
    check("no Traceback",     "Traceback" not in p.stdout + p.stderr)
    check("non-empty output", p.stdout.strip() != "")
    check("annotation icon",  "✓" in p.stdout or "🔍" in p.stdout)
    check("shows filename",   "events.py" in p.stdout)
    check("shows Gemini/",    "Gemini/" in p.stdout)
    check("finished < 30s",   elapsed < 30, f"took {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═"*60)
    print("  Code-Review Hook — Verification Suite")
    print("  Provider: Gemini only (or silent no-op if key not set)")
    if LIVE:
        print(f"  Live key: {LIVE_KEY[:12]}...")
    else:
        print("  Mode    : mock only  (GEMINI_REVIEWER_API_KEY not set)")
    print("═"*60)

    test_exclusions()
    test_error_resilience()
    test_happy_paths()
    test_model_override()
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
