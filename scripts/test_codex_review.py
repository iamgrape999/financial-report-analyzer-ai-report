#!/usr/bin/env python3
"""Verification & live test for the Codex review hook.

Usage
-----
  # With a real key (genuine live test against OpenAI API):
  OPENAI_API_KEY=sk-proj-... python3 scripts/test_codex_review.py

  # Without a key (mock-only, verifies all non-network branches):
  python3 scripts/test_codex_review.py

Exit codes
----------
  0  all tests passed
  1  one or more tests failed
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
import urllib.error
import urllib.request

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "codex_review.py")

# Representative target files in this repo
PROD_FILE  = os.path.join(ROOT, "credit_report", "audit", "events.py")   # 63 lines
BIG_FILE   = os.path.join(ROOT, "credit_report", "api", "reports.py")    # 1 000+ lines
TEST_FILE  = os.path.join(ROOT, "tests", "test_gap_coverage.py")
HTML_FILE  = os.path.join(ROOT, "static", "index.html")

API_KEY = os.getenv("OPENAI_API_KEY", "")
LIVE    = bool(API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(script: str, *, file_path: str = "", extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    if file_path:
        env["CLAUDE_TOOL_INPUT_FILE_PATH"] = file_path
    return subprocess.run([sys.executable, script], env=env,
                          capture_output=True, text=True)


RESULTS: list[tuple[bool, str, str]] = []   # (passed, desc, detail)


def check(desc: str, passed: bool, detail: str = "") -> None:
    icon = "✅" if passed else "❌"
    RESULTS.append((passed, desc, detail))
    print(f"  {icon}  {desc}" + (f"\n       {detail}" if detail and not passed else ""))


def section(title: str) -> None:
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")


# ─────────────────────────────────────────────────────────────────────────────
# Mock OpenAI HTTP server
# ─────────────────────────────────────────────────────────────────────────────

_QUEUE: list[str] = []

_CLEAN_BODY = json.dumps({
    "choices": [{"message": {"content": "✓ No critical issues."}}]
})
_WARN_BODY = json.dumps({
    "choices": [{"message": {"content":
        "• Unauthenticated endpoint exposes user emails — add require_analyst.\n"
        "• `db.flush()` missing before conflict detection query.\n"
        "• `AuditEvent.timestamp` has no index — full-table sort on GET /audit."
    }}]
})
_MALFORMED_BODY = b"not-json{"


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = _QUEUE.pop(0) if _QUEUE else _CLEAN_BODY
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


_srv = http.server.HTTPServer(("127.0.0.1", 0), _MockHandler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_MOCK_URL = f"http://127.0.0.1:{_port}/v1/chat/completions"


def _patched_script() -> str:
    """Return path to a temp copy of the script that calls our mock server."""
    with open(SCRIPT) as f:
        src = f.read()
    src = src.replace('"https://api.openai.com/v1/chat/completions"',
                      f'"{_MOCK_URL}"')
    tf = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False)
    tf.write(src)
    tf.flush()
    return tf.name


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_exclusion_branches() -> None:
    section("A — Exclusion / no-op branches  (no API call expected)")

    def run_no_key(**kw: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        return _run(SCRIPT, extra_env=env, **kw)

    print("\n  [1] No OPENAI_API_KEY")
    p = run_no_key(file_path=PROD_FILE)
    check("exit 0", p.returncode == 0)
    check("produces no output", p.stdout.strip() == "")

    print("\n  [2] No CLAUDE_TOOL_INPUT_FILE_PATH")
    p = _run(SCRIPT, extra_env={"OPENAI_API_KEY": "sk-fake"})
    check("exit 0", p.returncode == 0)
    check("produces no output", p.stdout.strip() == "")

    print("\n  [3] Non-Python file (.html)")
    p = _run(SCRIPT, file_path=HTML_FILE, extra_env={"OPENAI_API_KEY": "sk-fake"})
    check("exit 0", p.returncode == 0)
    check("produces no output", p.stdout.strip() == "")

    print("\n  [4] Test file excluded")
    p = _run(SCRIPT, file_path=TEST_FILE, extra_env={"OPENAI_API_KEY": "sk-fake"})
    check("exit 0", p.returncode == 0)
    check("produces no output", p.stdout.strip() == "")

    print("\n  [5] Scripts dir excluded")
    p = _run(SCRIPT,
             file_path=os.path.join(ROOT, "scripts", "seed_admin.py"),
             extra_env={"OPENAI_API_KEY": "sk-fake"})
    check("exit 0", p.returncode == 0)
    check("produces no output", p.stdout.strip() == "")

    print("\n  [6] File > MAX_LINES → skip message (limit forced to 10)")
    p = _run(SCRIPT, file_path=BIG_FILE,
             extra_env={"OPENAI_API_KEY": "sk-fake", "CODEX_REVIEW_MAX_LINES": "10"})
    check("exit 0", p.returncode == 0)
    check("prints skip notice", "skipped" in p.stdout.lower(), p.stdout[:120])
    if p.stdout.strip():
        print(f"       → {p.stdout.strip()}")

    print("\n  [7] Nonexistent file → silent")
    p = _run(SCRIPT, file_path=os.path.join(ROOT, "credit_report", "ghost.py"),
             extra_env={"OPENAI_API_KEY": "sk-fake"})
    check("exit 0", p.returncode == 0)
    check("produces no output", p.stdout.strip() == "")


def test_api_error_branches() -> None:
    section("B — API error branches  (network failures must not crash)")

    print("\n  [8] Unreachable API (real URL, no key → ConnectError)")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env={"OPENAI_API_KEY": "sk-fake"})
    check("exit 0 on network error", p.returncode == 0)
    check("no Traceback in output", "Traceback" not in p.stdout + p.stderr)

    print("\n  [9] Malformed JSON response")
    patched = _patched_script()
    try:
        _QUEUE.clear()
        _QUEUE.append(_MALFORMED_BODY)  # type: ignore[arg-type]
        p = _run(patched, file_path=PROD_FILE, extra_env={"OPENAI_API_KEY": "sk-fake"})
        check("exit 0 on bad JSON", p.returncode == 0)
        check("no Traceback", "Traceback" not in p.stdout + p.stderr)
    finally:
        os.unlink(patched)


def test_happy_paths_mock() -> None:
    section("C — Happy paths  (mock server, no real API cost)")

    patched = _patched_script()
    try:
        print("\n  [10] Clean file — model returns ✓ No critical issues")
        _QUEUE.clear()
        _QUEUE.append(_CLEAN_BODY)
        p = _run(patched, file_path=PROD_FILE, extra_env={"OPENAI_API_KEY": "sk-fake"})
        check("exit 0", p.returncode == 0)
        check("prints ✓ annotation", "✓" in p.stdout, p.stdout[:200])
        check("shows filename",      "events.py" in p.stdout)
        check("no 🔍 icon",          "🔍" not in p.stdout)
        print(f"       Output: {p.stdout.strip()}")

        print("\n  [11] File with issues — model returns bullet warnings")
        _QUEUE.clear()
        _QUEUE.append(_WARN_BODY)
        p = _run(patched, file_path=PROD_FILE, extra_env={"OPENAI_API_KEY": "sk-fake"})
        check("exit 0",              p.returncode == 0)
        check("prints 🔍 annotation","🔍" in p.stdout, p.stdout[:200])
        check("shows filename",      "events.py" in p.stdout)
        check("contains bullets",    "•" in p.stdout)
        print(f"       Output:\n{p.stdout.strip()}")
    finally:
        os.unlink(patched)


def test_live_openai() -> None:
    section("D — Live OpenAI call  (genuine gpt-4o-mini review)")

    if not LIVE:
        print("\n  ⚠️  OPENAI_API_KEY not set — skipping live tests.")
        print("  Set it and re-run to perform a genuine API call.\n")
        return

    print(f"\n  Key detected: {API_KEY[:8]}...{API_KEY[-4:]}")
    print(f"  Model: {os.getenv('CODEX_REVIEW_MODEL','gpt-4o-mini')}")
    print(f"  Target: credit_report/audit/events.py  ({sum(1 for _ in open(PROD_FILE))} lines)")

    t0 = time.monotonic()
    p = _run(SCRIPT, file_path=PROD_FILE)
    elapsed = time.monotonic() - t0

    print(f"\n  Response ({elapsed:.1f}s):\n")
    print("  " + "\n  ".join(p.stdout.strip().splitlines()))

    check("exit 0",                  p.returncode == 0,     p.stderr[:200] if p.returncode else "")
    check("no Traceback",            "Traceback" not in p.stdout + p.stderr)
    check("non-empty output",        p.stdout.strip() != "",  "(got empty string)")
    check("contains annotation icon","✓" in p.stdout or "🔍" in p.stdout)
    check("shows filename",          "events.py" in p.stdout)
    check("finished < 30s",          elapsed < 30,           f"took {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═"*60)
    print("  Codex Review Hook — Verification Suite")
    if LIVE:
        print(f"  Mode: LIVE  (key: {API_KEY[:8]}...)")
    else:
        print("  Mode: MOCK  (set OPENAI_API_KEY for live test)")
    print("═"*60)

    test_exclusion_branches()
    test_api_error_branches()
    test_happy_paths_mock()
    test_live_openai()

    _srv.shutdown()

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = sum(1 for ok, _, _ in RESULTS if not ok)

    print("\n" + "═"*60)
    print(f"  {'✅' if failed == 0 else '❌'}  {passed} passed, {failed} failed"
          f"  ({len(RESULTS)} assertions)")
    if failed:
        print("\n  Failed assertions:")
        for ok, desc, detail in RESULTS:
            if not ok:
                print(f"    ❌  {desc}  {detail}")
    print("═"*60 + "\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
