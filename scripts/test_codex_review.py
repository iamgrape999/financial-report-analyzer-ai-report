#!/usr/bin/env python3
"""Verification & live test for the multi-provider code-review hook.

Usage
-----
  # Mock only (no API cost — always works):
  python3 scripts/test_codex_review.py

  # Live test (uses whichever key is set — Gemini preferred):
  GEMINI_API_KEY=AIza...         python3 scripts/test_codex_review.py
  OPENROUTER_API_KEY=sk-or-...   python3 scripts/test_codex_review.py
  CEREBRAS_API_KEY=csk-...       python3 scripts/test_codex_review.py
  GROQ_API_KEY=gsk_...           python3 scripts/test_codex_review.py

Provider priority: Gemini → OpenRouter → Cerebras → Groq (first key wins).
Gemini is the only provider reachable from Claude Code iOS/web sandbox.

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

# Detect live provider (mirrors hook priority)
_GM_KEY  = os.getenv("GEMINI_API_KEY", "")
_OR_KEY  = os.getenv("OPENROUTER_API_KEY", "")
_CB_KEY  = os.getenv("CEREBRAS_API_KEY", "")
_GQ_KEY  = os.getenv("GROQ_API_KEY", "")

if _GM_KEY:
    LIVE_PROVIDER, LIVE_KEY = "Gemini",     _GM_KEY
elif _OR_KEY:
    LIVE_PROVIDER, LIVE_KEY = "OpenRouter", _OR_KEY
elif _CB_KEY:
    LIVE_PROVIDER, LIVE_KEY = "Cerebras",   _CB_KEY
elif _GQ_KEY:
    LIVE_PROVIDER, LIVE_KEY = "Groq",       _GQ_KEY
else:
    LIVE_PROVIDER, LIVE_KEY = "", ""

LIVE = bool(LIVE_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# Mock HTTP server  (handles both Gemini and OpenAI-compatible formats)
# ─────────────────────────────────────────────────────────────────────────────

_QUEUE: list[str] = []

# OpenAI-compatible response format
_CLEAN = json.dumps({"choices": [{"message": {"content": "✓ No critical issues."}}]})
_WARN  = json.dumps({"choices": [{"message": {"content":
    "• `mark_resolved` does not verify report ownership — IDOR possible.\n"
    "• Missing `await db.flush()` before conflict state re-query.\n"
    "• No approved-report immutability guard on conflict endpoints."}}]})

# Gemini response format
_CLEAN_GEMINI = json.dumps({
    "candidates": [{"content": {"parts": [{"text": "✓ No critical issues."}]}}]
})
_WARN_GEMINI = json.dumps({"candidates": [{"content": {"parts": [{"text":
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

# Provider URLs to patch → mock server
_PATCH_URLS = {
    "gemini":     ("https://generativelanguage.googleapis.com",
                   f"http://127.0.0.1:{_port}"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions",
                   f"http://127.0.0.1:{_port}/v1/chat/completions"),
    "cerebras":   ("https://api.cerebras.ai/v1/chat/completions",
                   f"http://127.0.0.1:{_port}/v1/chat/completions"),
    "groq":       ("https://api.groq.com/openai/v1/chat/completions",
                   f"http://127.0.0.1:{_port}/v1/chat/completions"),
}


def _patched(provider: str) -> str:
    """Return path to a temp copy whose provider URL points at mock server."""
    with open(SCRIPT) as f:
        src = f.read()
    old, new = _PATCH_URLS[provider]
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


_NO_KEYS = {
    "GEMINI_API_KEY":     "",
    "OPENROUTER_API_KEY": "",
    "CEREBRAS_API_KEY":   "",
    "GROQ_API_KEY":       "",
}


# ─────────────────────────────────────────────────────────────────────────────
# Test groups
# ─────────────────────────────────────────────────────────────────────────────

def test_exclusions() -> None:
    section("A — Exclusion / no-op branches  (zero API calls)")

    print("\n  [1] No API keys set")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env=_NO_KEYS)
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [2] No file path set")
    p = _run(SCRIPT, extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")

    print("\n  [3] HTML file excluded")
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

    print("\n  [6] Nonexistent file → silent")
    p = _run(SCRIPT, file_path=os.path.join(ROOT, "credit_report", "ghost.py"),
             extra_env={"GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no output", p.stdout.strip() == "")


def test_error_resilience() -> None:
    section("B — Error resilience  (API failures must never crash)")

    print("\n  [7] Unreachable Gemini URL → silent")
    p = _run(SCRIPT, file_path=PROD_FILE, extra_env={**_NO_KEYS, "GEMINI_API_KEY": "fake"})
    check("exit 0", p.returncode == 0)
    check("no Traceback", "Traceback" not in p.stdout + p.stderr)

    print("\n  [8] Malformed JSON from Gemini → silent")
    patched = _patched("gemini")
    try:
        _QUEUE.clear(); _QUEUE.append(b"not-json{")  # type: ignore[arg-type]
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={**_NO_KEYS, "GEMINI_API_KEY": "fake"})
        check("exit 0", p.returncode == 0)
        check("no Traceback", "Traceback" not in p.stdout + p.stderr)
    finally:
        os.unlink(patched)

    print("\n  [9] Malformed JSON from OpenAI-compatible → silent")
    patched = _patched("openrouter")
    try:
        _QUEUE.clear(); _QUEUE.append(b"not-json{")  # type: ignore[arg-type]
        p = _run(patched, file_path=PROD_FILE,
                 extra_env={**_NO_KEYS, "OPENROUTER_API_KEY": "fake"})
        check("exit 0", p.returncode == 0)
        check("no Traceback", "Traceback" not in p.stdout + p.stderr)
    finally:
        os.unlink(patched)


def test_provider_mock(provider_key: str, env_key: str, env_val: str,
                       provider_label: str, model_fragment: str) -> None:
    section(f"C.{provider_label} — Happy paths via mock server")
    is_gemini = provider_key == "gemini"
    patched   = _patched(provider_key)
    env       = {**_NO_KEYS, env_key: env_val,
                 "CODEX_REVIEW_MODEL": model_fragment if is_gemini
                 else f"test/{model_fragment}:free"}
    try:
        print(f"\n  [clean] {provider_label} returns ✓ No critical issues")
        _QUEUE.clear()
        _QUEUE.append(_CLEAN_GEMINI if is_gemini else _CLEAN)
        p = _run(patched, file_path=PROD_FILE, extra_env=env)
        check("exit 0",            p.returncode == 0)
        check("prints ✓",          "✓" in p.stdout,              p.stdout[:200])
        check("shows filename",    "events.py" in p.stdout)
        check("shows provider",    provider_label in p.stdout)
        check("shows model",       model_fragment.split("/")[-1] in p.stdout)
        check("no 🔍",             "🔍" not in p.stdout)
        print(f"       Output: {p.stdout.strip()}")

        print(f"\n  [warn]  {provider_label} returns issues found")
        _QUEUE.clear()
        _QUEUE.append(_WARN_GEMINI if is_gemini else _WARN)
        p = _run(patched, file_path=PROD_FILE, extra_env=env)
        check("exit 0",            p.returncode == 0)
        check("prints 🔍",         "🔍" in p.stdout,              p.stdout[:200])
        check("shows filename",    "events.py" in p.stdout)
        check("shows provider",    provider_label in p.stdout)
        check("contains bullets",  "•" in p.stdout)
        print(f"       Output:\n{p.stdout.strip()}")
    finally:
        os.unlink(patched)


def test_priority_fallback() -> None:
    section("D — Provider priority fallback")

    print("\n  [Gemini takes top priority over all others]")
    patched = _patched("gemini")
    try:
        _QUEUE.clear(); _QUEUE.append(_CLEAN_GEMINI)
        p = _run(patched, file_path=PROD_FILE, extra_env={
            "GEMINI_API_KEY":     "fake-gemini",
            "OPENROUTER_API_KEY": "fake-openrouter",
            "CEREBRAS_API_KEY":   "fake-cerebras",
            "GROQ_API_KEY":       "fake-groq",
            "CODEX_REVIEW_MODEL": "gemini-2.5-pro",
        })
        check("exit 0",         p.returncode == 0)
        check("uses Gemini",    "Gemini/" in p.stdout, p.stdout[:200])
        print(f"       Output: {p.stdout.strip()}")
    finally:
        os.unlink(patched)

    print("\n  [only OpenRouter key → uses OpenRouter]")
    patched = _patched("openrouter")
    try:
        _QUEUE.clear(); _QUEUE.append(_CLEAN)
        p = _run(patched, file_path=PROD_FILE, extra_env={
            **_NO_KEYS,
            "OPENROUTER_API_KEY": "fake-openrouter",
            "CODEX_REVIEW_MODEL": "test/or-model:free",
        })
        check("exit 0",             p.returncode == 0)
        check("uses OpenRouter",    "OpenRouter/" in p.stdout, p.stdout[:200])
        print(f"       Output: {p.stdout.strip()}")
    finally:
        os.unlink(patched)

    print("\n  [only Cerebras key → uses Cerebras]")
    patched = _patched("cerebras")
    try:
        _QUEUE.clear(); _QUEUE.append(_CLEAN)
        p = _run(patched, file_path=PROD_FILE, extra_env={
            **_NO_KEYS,
            "CEREBRAS_API_KEY":   "fake-cerebras",
            "CODEX_REVIEW_MODEL": "test/cerebras-model:free",
        })
        check("exit 0",          p.returncode == 0)
        check("uses Cerebras",   "Cerebras/" in p.stdout, p.stdout[:200])
        print(f"       Output: {p.stdout.strip()}")
    finally:
        os.unlink(patched)

    print("\n  [only Groq key → uses Groq]")
    patched = _patched("groq")
    try:
        _QUEUE.clear(); _QUEUE.append(_CLEAN)
        p = _run(patched, file_path=PROD_FILE, extra_env={
            **_NO_KEYS,
            "GROQ_API_KEY":       "fake-groq",
            "CODEX_REVIEW_MODEL": "test/groq-model:free",
        })
        check("exit 0",       p.returncode == 0)
        check("uses Groq",    "Groq/" in p.stdout, p.stdout[:200])
        print(f"       Output: {p.stdout.strip()}")
    finally:
        os.unlink(patched)


def test_live() -> None:
    section(f"E — Live call  (real {LIVE_PROVIDER or 'n/a'} API)")

    if not LIVE:
        print(
            "\n  ⚠️  No API key found — live section skipped.\n"
            "\n  Gemini works from Claude Code iOS/web (Anthropic sandbox):\n"
            "    GEMINI_API_KEY=AIza...   ← set this in your .env\n"
            "\n  These work from local CLI only (sandbox blocks them):\n"
            "    OPENROUTER_API_KEY=sk-or-...\n"
            "    CEREBRAS_API_KEY=csk-...\n"
            "    GROQ_API_KEY=gsk_...\n"
        )
        return

    n_lines = sum(1 for _ in open(PROD_FILE))
    print(f"\n  Provider : {LIVE_PROVIDER}")
    print(f"  Key      : {LIVE_KEY[:12]}...{LIVE_KEY[-4:]}")
    print(f"  Target   : credit_report/audit/events.py  ({n_lines} lines)")

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
    check("shows provider tag",  any(t in p.stdout for t in
                                     ("Gemini/", "OpenRouter/", "Cerebras/", "Groq/")))
    check("finished < 30s",      elapsed < 30, f"took {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═"*60)
    print("  Code-Review Hook — Verification Suite")
    print("  Priority: Gemini → OpenRouter → Cerebras → Groq")
    if LIVE:
        print(f"  Live provider : {LIVE_PROVIDER}  ({LIVE_KEY[:12]}...)")
    else:
        print("  Mode          : mock only  (no API keys set)")
    print("═"*60)

    test_exclusions()
    test_error_resilience()

    test_provider_mock("gemini",     "GEMINI_API_KEY",     "fake-gm",
                       "Gemini",     "gemini-2.5-pro")
    test_provider_mock("openrouter", "OPENROUTER_API_KEY", "fake-or",
                       "OpenRouter", "laguna-m.1")
    test_provider_mock("cerebras",   "CEREBRAS_API_KEY",   "fake-cb",
                       "Cerebras",   "llama3.1-70b")
    test_provider_mock("groq",       "GROQ_API_KEY",       "fake-gq",
                       "Groq",       "llama-3.3")

    test_priority_fallback()
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
