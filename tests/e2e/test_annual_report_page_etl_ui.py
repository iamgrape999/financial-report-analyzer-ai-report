from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from .utils_e2e import ARTIFACT_DIR, count_section7_core_metrics, is_identity_only_payload, require_real_file, save_json_artifact, save_text_artifact

pytestmark = pytest.mark.e2e


def _dump_failure_artifacts(page: Page, name: str, console_errors: list[str], failed_network: list[dict[str, Any]]) -> None:
    try:
        page.screenshot(path=str(ARTIFACT_DIR / f"{name}_failure.png"), full_page=True)
    except Exception:
        pass
    try:
        save_text_artifact(f"{name}_failure.html", page.content())
    except Exception:
        pass
    try:
        visible_modal = page.locator(".modal.show").inner_text(timeout=1000)
    except Exception:
        visible_modal = "<no visible modal>"
    save_json_artifact(f"{name}_failure_debug.json", {"console_errors": console_errors, "failed_network": failed_network, "visible_modal_text": visible_modal})


def _api_fetch(page: Page, path: str, method: str = "GET") -> Any:
    return page.evaluate(
        """async ({path, method}) => {
            const res = await fetch('/api/credit-report' + path, {method, headers: {Authorization: 'Bearer ' + localStorage.getItem('token')}});
            const text = await res.text();
            let body; try { body = JSON.parse(text); } catch (_) { body = text; }
            return {ok: res.ok, status: res.status, body};
        }""",
        {"path": path, "method": method},
    )


def _set_trace(context, enabled: bool) -> None:
    if enabled:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)


def test_ui_full_page_first_annual_report_flow(page: Page, context, e2e_base_url, e2e_credentials, tsmc_annual_report_path):
    pdf_path = require_real_file(tsmc_annual_report_path, "TSMC_ANNUAL_REPORT_PATH")
    timeout_ms = int(os.getenv("E2E_TIMEOUT_MS", "900000"))
    page.set_default_timeout(timeout_ms)
    console_errors: list[str] = []
    failed_network: list[dict[str, Any]] = []
    trace_path = ARTIFACT_DIR / "playwright_page_first_etl_trace.zip"
    _set_trace(context, True)

    def on_console(msg):
        if msg.type in {"error", "assert"}:
            console_errors.append(f"{msg.type}: {msg.text}")

    def on_response(response):
        if response.status >= 400:
            failed_network.append({"status": response.status, "url": response.url})

    page.on("console", on_console)
    page.on("response", on_response)

    try:
        # UI Test 1: login and create report.
        page.goto(e2e_base_url)
        page.get_by_test_id("login-email").fill(e2e_credentials["email"])
        page.get_by_test_id("login-password").fill(e2e_credentials["password"])
        page.get_by_test_id("login-submit").click()
        page.wait_for_function("() => document.querySelector('#viewDashboard')?.classList.contains('active')")
        title = "E2E_TSMC_PAGE_ETL_UI_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        page.get_by_test_id("create-report").click()
        page.get_by_test_id("report-title").fill(title)
        page.locator("#nType").fill("E2E Page-first Annual Report UI")
        page.locator("#nBranch").fill("E2E")
        page.locator("#createModal .btn-primary").click()
        page.wait_for_function("() => document.querySelector('#viewReport')?.classList.contains('active')")
        expect(page.locator("#rptTitle")).to_contain_text(title)
        report_id = page.evaluate("currentRid")
        save_json_artifact("ui_created_report.json", {"report_id": report_id, "title": title})
        assert report_id, "UI did not set currentRid after report creation."

        # UI Test 2: upload annual report with intentionally wrong type and verify Page ETL is offered.
        page.get_by_test_id("document-type-select").select_option("financial_statement")
        page.get_by_test_id("document-upload-input").set_input_files(str(pdf_path))
        page.locator("#uploadFileBtn").click()
        page.wait_for_function("() => Array.isArray(window.uploadedDocs) && window.uploadedDocs.length > 0", timeout=timeout_ms)
        doc_info = page.evaluate("uploadedDocs[0]")
        save_json_artifact("ui_uploaded_document.json", doc_info)
        assert doc_info.get("document_type") == "annual_report", f"UI upload did not show annual_report after wrong selection: {doc_info}"
        expect(page.get_by_test_id("page-etl-button").first).to_be_visible()
        doc_card_text = page.locator("#docList").inner_text()
        assert "Page ETL" in doc_card_text and "ETL" in doc_card_text, f"Annual report card did not expose Page ETL: {doc_card_text}"
        page.screenshot(path=str(ARTIFACT_DIR / "uploaded_annual_report_card.png"), full_page=True)

        # UI Test 3: run Page ETL and inspect modal/proposals.
        page.get_by_test_id("page-etl-button").first.click()
        expect(page.get_by_test_id("etl-modal")).to_be_visible()
        page.wait_for_selector("text=/Scan complete|掃描完成/", timeout=timeout_ms)
        modal_text = page.get_by_test_id("etl-modal").inner_text()
        save_text_artifact("page_etl_modal_scan_text.txt", modal_text)
        page.screenshot(path=str(ARTIFACT_DIR / "page_etl_modal_steps.png"), full_page=True)
        import re
        match = re.search(r"Scan complete:\s*(\d+)\s*/\s*(\d+)", modal_text) or re.search(r"掃描完成：\s*(\d+)\s*/\s*(\d+)", modal_text)
        assert match, f"Could not parse scan completion counts from modal: {modal_text}"
        processed, total = int(match.group(1)), int(match.group(2))
        assert processed == total and total > 0, f"UI scan did not process all pages: {processed}/{total}"
        page.wait_for_selector("text=/Plan ready|規劃完成/", timeout=timeout_ms)
        page.wait_for_selector("text=/Page-first ETL complete|逐頁 ETL 完成|Page-first ETL failed|逐頁 ETL 失敗/", timeout=timeout_ms)
        page.screenshot(path=str(ARTIFACT_DIR / "smart_import_proposals.png"), full_page=True)
        final_modal_text = page.get_by_test_id("etl-modal").inner_text()
        save_text_artifact("page_etl_modal_final_text.txt", final_modal_text)
        assert "500" not in final_modal_text, f"Page ETL modal shows server 500: {final_modal_text}"
        assert "failed" not in final_modal_text.lower() or "low" in final_modal_text.lower() or "coverage" in final_modal_text.lower(), (
            f"Page ETL failed for a reason other than low coverage: {final_modal_text}"
        )

        proposals_res = _api_fetch(page, f"/reports/{report_id}/smart-import/proposals?doc_id={doc_info['id']}", "POST")
        assert proposals_res["ok"], f"Could not fetch proposals through UI auth: {proposals_res}"
        proposals = proposals_res["body"]
        save_json_artifact("ui_smart_import_proposals.json", proposals)
        assert proposals, "UI Page ETL produced no Smart Import proposals."
        assert all((p.get("evidence_map") or {}).get("source_pages") for p in proposals), f"Some proposals lack source pages: {proposals}"

        # UI Test 4: Section 7 proposal validation.
        section7 = [p for p in proposals if p.get("section_no") == 7]
        assert section7, f"Section 7 has no proposal after annual report Page ETL: {proposals}"
        for proposal in section7:
            payload = proposal.get("proposed_json") or {}
            metric_count, found, missing = count_section7_core_metrics(payload)
            save_json_artifact(f"ui_section7_proposal_{proposal['id']}_metrics.json", {"metric_count": metric_count, "found": found, "missing": missing, "proposal": proposal})
            if proposal.get("status") == "ready_for_review":
                assert metric_count >= 5, f"Ready Section 7 proposal has too few core metrics: found={found}, missing={missing}"
                assert not is_identity_only_payload(payload), f"Identity-only Section 7 proposal marked ready: {proposal}"
            if is_identity_only_payload(payload):
                assert proposal.get("status") == "low_coverage_failed", f"Identity-only proposal must be low_coverage_failed: {proposal}"

        # UI Test 5: commit ready proposal, reload, verify API and UI persistence.
        ready = [p for p in section7 if p.get("status") == "ready_for_review"]
        assert ready, f"No ready_for_review Section 7 proposal available to commit; cannot claim /inputs/7 persistence. proposals={proposals}"
        ready_proposal = ready[0]
        metric_count, _, _ = count_section7_core_metrics(ready_proposal.get("proposed_json") or {})
        assert metric_count >= 5, "Will not commit a Section 7 ready proposal that lacks financial metrics."
        card = page.locator("[data-testid='smart-import-proposal-card']").filter(has_text=f"§{ready_proposal['section_no']}").filter(has_text="ready_for_review").first
        card.get_by_test_id("smart-import-commit-button").click()
        page.wait_for_timeout(1000)
        commit_check = _api_fetch(page, f"/reports/{report_id}/inputs/{ready_proposal['section_no']}")
        assert commit_check["ok"], f"Commit toast/UI is insufficient; API input missing after commit: {commit_check}"
        save_json_artifact("ui_section_input_after_commit.json", commit_check["body"])
        page.reload(wait_until="networkidle")
        page.evaluate(f"loadReportDetail('{report_id}')")
        page.wait_for_function("() => document.querySelector('#viewReport')?.classList.contains('active')")
        reload_check = _api_fetch(page, f"/reports/{report_id}/inputs/{ready_proposal['section_no']}")
        assert reload_check["ok"], f"SectionInput disappeared after reload: {reload_check}"
        save_json_artifact("section7_input_after_reload.json", reload_check["body"])
        persisted_count, found, missing = count_section7_core_metrics(reload_check["body"].get("input_json") or {})
        assert persisted_count >= 5, f"Reloaded /inputs/7 lacks financial metrics: found={found}, missing={missing}"
        page.evaluate("openPanel(7, 'input')")
        expect(page.get_by_test_id("section-7-input")).to_be_visible()
        assert (found[0] if found else "revenue") in page.get_by_test_id("section-7-input").input_value()
        page.screenshot(path=str(ARTIFACT_DIR / "section7_after_commit.png"), full_page=True)
        assert not console_errors, f"Uncaught console errors observed: {console_errors}"
        unexplained_500s = [r for r in failed_network if r["status"] >= 500]
        assert not unexplained_500s, f"Unexplained network 5xx responses observed: {unexplained_500s}"
        save_json_artifact("ui_console_and_network.json", {"console_errors": console_errors, "failed_network": failed_network})
    except Exception:
        _dump_failure_artifacts(page, "ui_page_first_etl", console_errors, failed_network)
        raise
    finally:
        try:
            context.tracing.stop(path=str(trace_path))
        except Exception:
            pass
