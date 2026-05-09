#!/usr/bin/env python3
"""Browser-free smoke test for the built-in credit report UI.

The CI/agent environment may not have Chromium/Firefox installed, so this script
validates the UI mount, static assets, authentication, report creation, section
input save/load, and document list flow through HTTP only. It also checks that the
client script does not use template-based dynamic ``innerHTML`` rendering for
user-controlled fields.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = int(os.getenv("UI_SMOKE_PORT", "8766"))
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "secret123"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "id" and value:
                self.ids.add(value)


def _request(
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    token: str | None = None,
    timeout: int = 10,
) -> tuple[int, str]:
    data: bytes | None = None
    headers: dict[str, str] = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if form_body is not None:
        data = urlencode(form_body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as response:  # noqa: S310 - local smoke URL only
        return response.status, response.read().decode("utf-8")


def _wait_for_health(base_url: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited early with code {proc.returncode}")
        try:
            status, body = _request(f"{base_url}/health", timeout=2)
            if status == 200 and json.loads(body).get("ok") is True:
                return
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise TimeoutError(f"health endpoint did not become ready: {last_error}")


def _assert_static_ui(base_url: str) -> None:
    status, bare_html = _request(f"{base_url}/app")
    assert status == 200
    assert "AI 授信報告產生器" in bare_html

    status, html = _request(f"{base_url}/app/")
    assert status == 200
    assert "AI 授信報告產生器" in html

    parser = IdCollector()
    parser.feed(html)
    required_ids = {
        "loginForm",
        "email",
        "password",
        "logoutButton",
        "reportForm",
        "reportsList",
        "sectionNo",
        "sectionJson",
        "pdfFile",
        "generateOne",
        "generateAll",
        "markdownOutput",
        "toast",
    }
    missing = sorted(required_ids - parser.ids)
    if missing:
        raise AssertionError(f"missing UI element ids: {missing}")

    status, css = _request(f"{base_url}/app/styles.css")
    assert status == 200
    assert ".status-card" in css

    status, js = _request(f"{base_url}/app/app.js")
    assert status == 200
    assert "function logout()" in js
    for unsafe in ("innerHTML = `", "card.innerHTML", "chip.innerHTML"):
        if unsafe in js:
            raise AssertionError(f"unsafe dynamic HTML pattern remains: {unsafe}")


def _assert_api_flow(base_url: str) -> str:
    _, login_body = _request(
        f"{base_url}/api/credit-report/auth/login",
        method="POST",
        form_body={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    token = json.loads(login_body)["access_token"]

    report_payload = {
        "industry": "marine",
        "report_type": "ui_smoke",
        "borrower_name": "<img src=x onerror=alert(1)>",
        "booking_branch": "SG",
    }
    _, created_body = _request(
        f"{base_url}/api/credit-report/reports",
        method="POST",
        json_body=report_payload,
        token=token,
    )
    report_id = json.loads(created_body)["id"]

    _, listed_body = _request(f"{base_url}/api/credit-report/reports?limit=5", token=token)
    assert any(report["id"] == report_id for report in json.loads(listed_body))

    section_payload = {
        "section_no": 7,
        "input_json": {"financials": {"revenue_usd_m": 123, "ebitda_usd_m": 45}},
    }
    _request(
        f"{base_url}/api/credit-report/reports/{report_id}/inputs/7",
        method="PUT",
        json_body=section_payload,
        token=token,
    )
    _, loaded_body = _request(f"{base_url}/api/credit-report/reports/{report_id}/inputs/7", token=token)
    assert json.loads(loaded_body)["input_json"]["financials"]["revenue_usd_m"] == 123

    _, docs_body = _request(f"{base_url}/api/credit-report/reports/{report_id}/documents", token=token)
    assert json.loads(docs_body) == []
    return report_id


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="credit-report-ui-smoke-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "ui_smoke.db"
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
                "CREDIT_REPORTS_ROOT": str(tmp_path / "credit_reports"),
                "ENVIRONMENT": "development",
                "AUTO_CREATE_TABLES": "true",
                "ADMIN_EMAIL": ADMIN_EMAIL,
                "ADMIN_PASSWORD": ADMIN_PASSWORD,
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(DEFAULT_PORT)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{DEFAULT_PORT}"
            _wait_for_health(base_url, proc)
            _assert_static_ui(base_url)
            report_id = _assert_api_flow(base_url)
            print(json.dumps({"ui": "ok", "report_id": report_id}, ensure_ascii=False))
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
