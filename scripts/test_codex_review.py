#!/usr/bin/env python3
"""Verification & live test for the AI code-review hook.

Usage
-----
  # Mock only (no API cost — always works):
  python3 scripts/test_codex_review.py

  # Live test with whichever key you have:
  GEMINI_API_KEY=AIza...       python3 scripts/test_codex_review.py
  ANTHROPIC_API_KEY=sk-ant-... python3 scripts/test_codex_review.py
  OPENAI_API_KEY=sk-proj-...   python3 scripts/test_codex_review.py

Provider priority: Gemini → Anthropic → OpenAI (first key found wins).

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

# Detect which live key is available (mirrors hook priority)
_GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")
_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")

if _GEMINI_KEY:
    LIVE_PROVIDER, LIVE_KEY = "gemini",    _GEMINI_KEY
elif _ANTHROPIC_KEY:
    LIVE_PROVIDER, LIVE_KEY = "anthropic", _ANTHROPIC_KEY
elif _OPENAI_KEY:
    LIVE_PROVIDER, LIVE_KEY = "openai",    _OPENAI_KEY
else:
    LIVE_PROVIDER, LIVE_KEY = "",          ""

LIVE = bool(LIVE_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Mock HTTP server — handles all three provider formats
# ─────────────────────────────────────────────────────────────────────────────

_QUEUE: list[str] = []

_CLEAN = json.dumps({"choices":[{"message":{"content":"✓ No critical issues."}}]})
_WARN  = json.dumps({"choices":[{"message":{"content":
    "• Unauthenticated endpoint exposes user emails — add require_analyst.\n"
    "• `db.flush()` missing before conflict detection query.\n"
    "• No approved-report guard on write endpoints."}}]})

# Gemini response shape
_CLEAN_G = json.dumps({"candidates":[{"content":{"parts":[{"text":"✓ No critical issues."}]}}]})
_WARN_G  = json.dumps({"candidates":[{"content":{"parts":[{"text":
    "• Missing `await db.flush()` before conflict detection.\n"
    "• `AuditEvent.timestamp` not indexed — full-table sort.\n"
    "• No approved-report guard on PATCH /blocks."}]}}]})

# Anthropic response shape
_CLEAN_A = json.dumps({"content":[{"text":"✓ No critical issues."}]})
_WARN_A  = json.dumps({"content":[{"text":
    "• Race condition in `_generating_sections` under multi-worker Gunicorn.\n"
    "• JTI revocation dict evicts before token TTL expires.\n"
    "• IDOR possible on ETL stream without report ownership check."}]})


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


def _patched(provider: str) -> str:
    """Return path to a temp copy whose provider URL points at mock server."""
    with open(SCRIPT) as f:
        src = f.read()
    replacements = {
        "gemini":    ("https://generativelanguage.googleapis.com",
                      f"http://127.0.0.1:{_port}"),
        "anthropic": ("https://api.anthropic.com/v1/messages",
                      f"http://127.0.0.1:{_port}/v1/messages"),
        "openai":    ("https://api.openai.com/v1/chat/completions",
                      f"http://127.0.0.1:{_port}/v1/chat/completions"),
    }
    old, new = replacements[provider]
    src = src.replace(old, new)
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
    no_keys = {k: "" for k in ("GEMINI_API_KEY","ANTHROPIC_API_KEY","OPENAI_API_KEY")}

    print("\n  [1] No API keys set")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env=no_keys)
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [2] No file path set")
    p = _run(SCRIPT, extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [3] HTML file (.html) excluded")
    p = _run(SCRIPT, file_path=HTML_FILE, extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [4] Test file excluded")
    p = _run(SCRIPT, file_path=TEST_FILE, extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [5] File exceeds MAX_LINES (limit=10)")
    p = _run(SCRIPT, file_path=BIG_FILE,
             extra_env={"GEMINI_API_KEY": "fake", "CODEX_REVIEW_MAX_LINES": "10"})
    check("exit 0", p.returncode == 0)
    check("prints skip notice", "skipped" in p.stdout.lower(), p.stdout[:100])
    if p.stdout.strip():
        print(f"       → {p.stdout.strip()}")

    print("\n  [6] Nonexistent file → silent")
    p = _run(SCRIPT, file_path=os.path.join(ROOT,"credit_report","ghost.py"),
             extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")


def test_error_resilience() -> None:
    section("B — Error resilience  (API failures must never crash)")

    print("\n  [7] Unreachable API URL → silent")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no Traceback", "Traceback" not in p.stdout + p.stderr)

    print("\n  [8] Malformed JSON response → silent")
    patched = _patched("openai")
    try:
        _QUEUE.clear(); _QUEUE.append(b"not-json{")  # type: ignore[arg-type]
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={"OPENAI_API_KEY": "fake",
                             "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        check("exit 0", p.returncode == 0)
        check("no Traceback", "Traceback" not in p.stdout + p.stderr)
    finally:
        os.unlink(patched)


def test_provider_mock(provider: str, env_key: str, env_val: str,
                       clean_body: str, warn_body: str,
                       provider_tag: str) -> None:
    section(f"C.{provider.title()} — Happy paths via mock server")
    patched = _patched(provider)
    other_keys = {k: "" for k in ("GEMINI_API_KEY","ANTHROPIC_API_KEY","OPENAI_API_KEY")
                  if k != env_key}
    env = {env_key: env_val, **other_keys}
    try:
        print(f"\n  [clean] {provider_tag} returns ✓ No critical issues")
        _QUEUE.clear(); _QUEUE.append(clean_body)
        p = _run(patched, file_path=PROD_FILE, extra_env=env)
        check("exit 0",           p.returncode == 0)
        check("prints ✓",         "✓" in p.stdout,         p.stdout[:200])
        check("shows filename",   "events.py" in p.stdout)
        check("shows provider",   provider_tag in p.stdout)
        check("no 🔍",            "🔍" not in p.stdout)
        print(f"       Output: {p.stdout.strip()}")

        print(f"\n  [warn]  {provider_tag} returns issues found")
        _QUEUE.clear(); _QUEUE.append(warn_body)
        p = _run(patched, file_path=PROD_FILE, extra_env=env)
        check("exit 0",           p.returncode == 0)
        check("prints 🔍",        "🔍" in p.stdout,         p.stdout[:200])
        check("shows filename",   "events.py" in p.stdout)
        check("shows provider",   provider_tag in p.stdout)
        check("contains bullets", "•" in p.stdout)
        print(f"       Output:\n{p.stdout.strip()}")
    finally:
        os.unlink(patched)


def test_live() -> None:
    section(f"D — Live call  (real {LIVE_PROVIDER or 'n/a'} API)")

    if not LIVE:
        print(
            "\n  ⚠️  No API key found — live section skipped.\n"
            "\n  To run a genuine test, set one of:\n"
            "    GEMINI_API_KEY=AIza...        (already in this project)\n"
            "    ANTHROPIC_API_KEY=sk-ant-...  (platform.anthropic.com)\n"
            "    OPENAI_API_KEY=sk-proj-...    (platform.openai.com — separate from ChatGPT)\n"
        )
        return

    n_lines = sum(1 for _ in open(PROD_FILE))
    print(f"\n  Provider : {LIVE_PROVIDER}")
    print(f"  Key      : {LIVE_KEY[:8]}...{LIVE_KEY[-4:]}")
    print(f"  Target   : credit_report/audit/events.py  ({n_lines} lines)")

    t0 = time.monotonic()
    p = _run(SCRIPT, file_path=PROD_FILE)
    elapsed = time.monotonic() - t0

    print(f"\n  Response ({elapsed:.1f}s):\n")
    for line in p.stdout.strip().splitlines():
        print(f"  {line}")

    check("exit 0",              p.returncode == 0,  p.stderr[:300] if p.returncode else "")
    check("no Traceback",        "Traceback" not in p.stdout + p.stderr)
    check("non-empty output",    p.stdout.strip() != "")
    check("has annotation icon", "✓" in p.stdout or "🔍" in p.stdout)
    check("shows filename",      "events.py" in p.stdout)
    check("shows provider tag",  any(t in p.stdout for t in ("Gemini","Claude","GPT")))
    check("finished < 30s",      elapsed < 30,       f"took {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═"*60)
    print("  AI Code-Review Hook — Verification Suite")
    if LIVE:
        print(f"  Live provider : {LIVE_PROVIDER}  ({LIVE_KEY[:8]}...)")
    else:
        print("  Mode          : mock only  (no API keys set)")
    print("═"*60)

    test_exclusions()
    test_error_resilience()

    # Mock-server tests for all three providers
    test_provider_mock("gemini",    "GEMINI_API_KEY",    "fake-gemini",
                       _CLEAN_G, _WARN_G, "Gemini")
    test_provider_mock("anthropic", "ANTHROPIC_API_KEY", "fake-anthropic",
                       _CLEAN_A, _WARN_A, "Claude")
    test_provider_mock("openai",    "OPENAI_API_KEY",    "fake-openai",
                       _CLEAN,   _WARN,   "GPT-4o-mini")

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
