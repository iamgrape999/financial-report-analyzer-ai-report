from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from typing import Any

import httpx
import pytest
from dotenv import load_dotenv

from .utils_e2e import ARTIFACT_DIR, save_json_artifact

load_dotenv(".env.e2e", override=False)

API_PREFIX = "/api/credit-report"
RUN_CONTEXT: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "api_results": [],
    "ui_results": [],
    "network_4xx": [],
    "network_5xx": [],
    "js_errors": [],
    "blockers": [],
}


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _playwright_version() -> str:
    try:
        import playwright
        return getattr(playwright, "__version__", "installed")
    except Exception:
        return "not installed"


def pytest_addoption(parser):
    parser.addoption("--e2e-fail-missing-pdf", action="store_true", help="Fail instead of skip when TSMC_ANNUAL_REPORT_PATH is missing")


@pytest.fixture(scope="session")
def e2e_base_url() -> str:
    return os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def e2e_credentials() -> dict[str, str]:
    email = os.getenv("E2E_EMAIL", "admin@example.com")
    password = os.getenv("E2E_PASSWORD", "change-me")
    if not password or password == "change-me":
        pytest.skip("Set E2E_PASSWORD in .env.e2e; real E2E tests must use a real non-production test account.")
    return {"email": email, "password": password}


@pytest.fixture(scope="session")
def tsmc_annual_report_path(pytestconfig) -> Path:
    raw = os.getenv("TSMC_ANNUAL_REPORT_PATH")
    if not raw:
        msg = "TSMC_ANNUAL_REPORT_PATH is not set. Copy .env.e2e.example to .env.e2e and point it at the real TSMC annual-report PDF."
        if pytestconfig.getoption("--e2e-fail-missing-pdf"):
            pytest.fail(msg)
        pytest.skip(msg)
    p = Path(raw).expanduser()
    if not p.exists():
        msg = f"TSMC_ANNUAL_REPORT_PATH does not exist: {p}. Do not use fake PDFs for real E2E."
        if pytestconfig.getoption("--e2e-fail-missing-pdf"):
            pytest.fail(msg)
        pytest.skip(msg)
    if p.stat().st_size < 1024 * 1024:
        pytest.fail(f"TSMC annual-report file is suspiciously small ({p.stat().st_size} bytes): {p}")
    return p


@pytest.fixture(scope="session")
def api_client(e2e_base_url: str):
    timeout = httpx.Timeout(float(os.getenv("E2E_TIMEOUT_MS", "900000")) / 1000.0)
    with httpx.Client(base_url=e2e_base_url + API_PREFIX, timeout=timeout, follow_redirects=True) as client:
        yield client


@pytest.fixture(scope="session")
def auth_headers(api_client: httpx.Client, e2e_credentials: dict[str, str]) -> dict[str, str]:
    response = api_client.post(
        "/auth/login",
        data={"username": e2e_credentials["email"], "password": e2e_credentials["password"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    save_json_artifact("login_response.json", {"status_code": response.status_code, "body": response.text[:2000]})
    assert response.status_code == 200, f"Real login failed: {response.status_code} {response.text[:2000]}"
    token = response.json().get("access_token")
    assert token, f"Login response did not include access_token: {response.text[:2000]}"
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def e2e_report(api_client: httpx.Client, auth_headers: dict[str, str]) -> dict[str, Any]:
    title = "E2E_TSMC_PAGE_ETL_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {"industry": "corporate", "report_type": "E2E Page-first Annual Report", "borrower_name": title, "booking_branch": "E2E"}
    response = api_client.post("/reports", headers=auth_headers, json=payload)
    save_json_artifact("created_report_response.json", {"status_code": response.status_code, "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text})
    assert response.status_code == 201, f"Could not create E2E report: {response.status_code} {response.text[:2000]}"
    report = response.json()
    print(f"E2E report retained for inspection: {report['id']} ({title})")
    return report


def record_result(kind: str, name: str, result: str, evidence: str = "") -> None:
    RUN_CONTEXT.setdefault(kind, []).append({"name": name, "result": result, "evidence": evidence})


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when == "setup" and rep.skipped):
        marker = "ui_results" if "ui" in item.nodeid else "api_results"
        result = "PASS" if rep.passed else "SKIP" if rep.skipped else "FAIL"
        record_result(marker, item.name, result, item.nodeid)
        if rep.skipped:
            RUN_CONTEXT["blockers"].append(f"{item.nodeid}: skipped ({rep.longrepr})")
        if rep.failed:
            RUN_CONTEXT["blockers"].append(f"{item.nodeid}: {rep.longreprtext[-1200:]}")


def pytest_sessionfinish(session, exitstatus):
    RUN_CONTEXT["ended_at"] = datetime.now(timezone.utc).isoformat()
    base_url = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000")
    tsmc_path = os.getenv("TSMC_ANNUAL_REPORT_PATH", "<not set>")
    all_results = RUN_CONTEXT.get("api_results", []) + RUN_CONTEXT.get("ui_results", [])
    has_fail_or_skip = any(r.get("result") in {"FAIL", "SKIP"} for r in all_results) or bool(RUN_CONTEXT.get("blockers"))
    verdict = "Pass" if exitstatus == 0 and all_results and not has_fail_or_skip else ("Partial" if any(r.get("result") == "PASS" for r in all_results) else "Fail")
    lines = [
        "# Page-first Annual Report ETL E2E Report", "",
        "## Environment",
        f"- repo commit: {_git_commit()}",
        f"- base URL: {base_url}",
        f"- Python: {platform.python_version()}",
        f"- Playwright: {_playwright_version()}",
        f"- Browser: {'Chrome channel requested' if os.getenv('E2E_USE_REAL_CHROME','true').lower() == 'true' else 'Chromium'}",
        f"- TSMC annual report path: {tsmc_path}",
        f"- test started at: {RUN_CONTEXT['started_at']}",
        f"- test ended at: {RUN_CONTEXT['ended_at']}", "",
        "## API Test Results", "| Test | Result | Evidence |", "|---|---|---|",
    ]
    lines += [f"| {r['name']} | {r['result']} | {r['evidence']} |" for r in RUN_CONTEXT.get("api_results", [])]
    lines += ["", "## UI Test Results", "| Step | Result | Screenshot / Artifact |", "|---|---|---|"]
    lines += [f"| {r['name']} | {r['result']} | {r['evidence']} |" for r in RUN_CONTEXT.get("ui_results", [])]
    lines += [
        "", "## Annual Report Detection", "- selected document_type: captured in annual_report_detection_response.json", "- inferred document_type: captured in annual_report_detection_response.json", "- result: see API Test Results",
        "", "## Scan Pages", "- processed_pages: captured in scan_pages_response.json", "- total_pages: captured in scan_pages_response.json", "- coverage_pct: captured in scan_pages_response.json", "- financial_pages_detected: captured in scan_pages_response.json", "- result: see API Test Results",
        "", "## Section 7 Validation", "- proposal status: captured in proposals_after_etl.json", "- coverage_score: captured in proposals_after_etl.json", "- source pages: captured in proposals_after_etl.json", "- extracted metric families: captured in section7_* artifacts", "- missing metric families: captured in section7_* artifacts", "- identity-only rejected: see test_identity_only_section7_proposal_gate", "- empty proposal rejected: see test_empty_section7_proposal_gate",
        "", "## Smart Import Commit", "- proposal_id: captured in committed_ready_proposal.json", "- commit result: captured in committed_ready_proposal.json", "- section input persisted: captured in section7_input_after_commit*.json", "- reload verified: captured in section7_input_after_commit_reload.json",
        "", "## Network / Console Errors", f"- 4xx: {len(RUN_CONTEXT.get('network_4xx', []))}", f"- 5xx: {len(RUN_CONTEXT.get('network_5xx', []))}", f"- JS errors: {len(RUN_CONTEXT.get('js_errors', []))}",
        "", "## Final Verdict", verdict,
        "", "## Blockers",
    ]
    blockers = RUN_CONTEXT.get("blockers") or ["None recorded by pytest hooks. Review individual artifacts before claiming complete fix."]
    lines += [f"{i+1}. {b}" for i, b in enumerate(blockers[:20])]
    can_claim = verdict == "Pass" and os.getenv("TSMC_ANNUAL_REPORT_PATH") and os.getenv("E2E_PASSWORD") not in {None, "", "change-me"}
    lines += ["", "## Can we claim complete fix?", ("Yes, complete fix confirmed." if can_claim else "No. Any skipped/failed API/UI E2E step, missing credentials, or missing real TSMC PDF prevents a complete-fix claim.")]
    (ARTIFACT_DIR / "E2E_PAGE_FIRST_ETL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
