"""Regression tests for the deep-audit round (2026-05-29).

Covers the bounded, clearly-correct fixes applied after the 4-domain deep audit:
  1. Block read IDOR — get_block / block_history / list_cells now enforce report access
  2. ETL endpoints (etl_document, etl_document_stream) require owner/admin, not view
  3. gap_fill_report requires owner/admin
  4. generate_full_report blocks regeneration of APPROVED reports
  5. update_status enforces approved-immutability (no silent un-approve)
  6. net_debt_ebitda is computed as NET debt / EBITDA (cash netted) — not gross
  7. evidence save/load route through _safe_report_dir (path-traversal guard)
  8. register normalises email to lowercase (case-insensitive uniqueness)
  9. Frontend: escJs() neutralises single-quote JS-string-break in inline onclick
 10. Frontend: SSE URL helper fails closed (no raw JWT in query string)
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SRC_BLOCKS = (ROOT / "credit_report" / "api" / "blocks.py").read_text()
SRC_GENERATE = (ROOT / "credit_report" / "api" / "generate.py").read_text()
SRC_REPORTS = (ROOT / "credit_report" / "api" / "reports.py").read_text()
SRC_CALC = (ROOT / "credit_report" / "api" / "calculations.py").read_text()
SRC_AUTH = (ROOT / "credit_report" / "api" / "auth.py").read_text()
SRC_EVIDENCE = (ROOT / "credit_report" / "generation" / "evidence.py").read_text()
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class TestBlockReadIDOR:
    def test_get_block_enforces_report_access(self):
        fn = SRC_BLOCKS[SRC_BLOCKS.find("async def get_block("):]
        body = fn[:400]
        assert "_get_report_or_403" in body, "get_block must verify report access (IDOR fix)"

    def test_block_history_enforces_report_access(self):
        fn = SRC_BLOCKS[SRC_BLOCKS.find("async def block_history("):]
        body = fn[:400]
        assert "_get_report_or_403" in body, "block_history must verify report access (IDOR fix)"

    def test_list_cells_enforces_report_access(self):
        fn = SRC_BLOCKS[SRC_BLOCKS.find("async def list_cells("):]
        body = fn[:400]
        assert "_get_report_or_403" in body, "list_cells must verify report access (IDOR fix)"


class TestEtlAndGapFillOwnerCheck:
    def test_etl_document_endpoint_uses_owner_check(self):
        fn = SRC_GENERATE[SRC_GENERATE.find("async def etl_document_endpoint("):]
        body = fn[:900]
        assert "_assert_owner_or_admin" in body
        assert "_assert_can_view" not in body.split("_assert_owner_or_admin")[0]

    def test_etl_document_stream_uses_owner_check(self):
        idx = SRC_GENERATE.find("a task_id.  Connect to GET")  # docstring inside the stream endpoint
        fn = SRC_GENERATE[idx:idx + 600]
        assert "_assert_owner_or_admin" in fn
        assert "_assert_can_view" not in fn

    def test_gap_fill_uses_owner_check(self):
        # gap_fill_report sits just before its 'Approved reports cannot be modified' guard
        idx = SRC_GENERATE.find("Approved reports cannot be modified")
        window = SRC_GENERATE[idx - 300:idx]
        assert "_assert_owner_or_admin" in window
        assert "_assert_can_view" not in window


class TestGenerateFullReportImmutability:
    def test_full_report_blocks_approved(self):
        idx = SRC_GENERATE.find("async def generate_full_report(")
        body = SRC_GENERATE[idx:idx + 900]
        assert 'report.status == "approved"' in body, (
            "generate_full_report must 409 on approved reports (immutability)"
        )


class TestUpdateStatusImmutability:
    def test_update_status_blocks_unapprove(self):
        idx = SRC_REPORTS.find("async def update_status(")
        body = SRC_REPORTS[idx:idx + 1200]
        assert 'report.status == "approved"' in body, (
            "update_status must enforce approved-immutability (no silent un-approve)"
        )


class TestNetDebtEbitda:
    def test_function_exists(self):
        from credit_report.calculation_engine.financial_ratios import net_debt_to_ebitda
        # (1000 debt - 200 cash) / 400 ebitda = 2.0x
        val, formula, fids = net_debt_to_ebitda(1000.0, 200.0, 400.0, "d", "c", "e")
        assert val == pytest.approx(2.0)
        assert "Total Debt - Cash" in formula
        assert fids == ["d", "c", "e"]

    def test_net_is_lower_than_gross(self):
        from credit_report.calculation_engine.financial_ratios import (
            net_debt_to_ebitda, debt_to_ebitda,
        )
        net, _, _ = net_debt_to_ebitda(1000.0, 300.0, 200.0)
        gross, _, _ = debt_to_ebitda(1000.0, 200.0)
        assert net < gross, "net leverage must be below gross when cash is positive"

    def test_calc_endpoint_uses_net_when_cash_present(self):
        assert "net_debt_to_ebitda(" in SRC_CALC, "recalc must call net_debt_to_ebitda when cash exists"
        assert "if cash_f:" in SRC_CALC


class TestEvidencePathTraversalGuard:
    def test_save_text_uses_safe_dir(self):
        fn = SRC_EVIDENCE[SRC_EVIDENCE.find("def save_document_text("):]
        assert "_safe_report_dir(report_id)" in fn[:300]

    def test_save_binary_uses_safe_dir(self):
        fn = SRC_EVIDENCE[SRC_EVIDENCE.find("def save_document_binary("):]
        assert "_safe_report_dir(report_id)" in fn[:300]

    def test_load_texts_uses_safe_dir(self):
        fn = SRC_EVIDENCE[SRC_EVIDENCE.find("def load_document_texts("):]
        assert "_safe_report_dir(report_id)" in fn[:300]

    def test_safe_dir_rejects_traversal(self):
        from credit_report.generation.evidence import _safe_report_dir
        with pytest.raises(ValueError):
            _safe_report_dir("../../etc")


class TestEmailNormalisation:
    def test_register_lowercases_email(self):
        idx = SRC_AUTH.find("normalized_email = payload.email.strip().lower()")
        assert idx != -1, "register must normalise email to lowercase"
        # duplicate check must be case-insensitive
        assert "func.lower(User.email) == normalized_email" in SRC_AUTH


class TestFrontendXssEscJs:
    def test_escjs_defined(self):
        assert "function escJs(" in HTML

    def test_escjs_escapes_quote_and_backslash(self):
        # the helper must replace backslash then single-quote before HTML-escaping
        idx = HTML.find("function escJs(")
        body = HTML[idx:idx + 200]
        assert "replace(/\\\\/g" in body  # backslash escaping
        assert "/'/g" in body              # single-quote escaping

    def test_vulnerable_sinks_use_escjs(self):
        assert "deleteReport('${r.id}','${escJs(r.borrower_name||r.id)}')" in HTML
        assert "etlDocument('${d.id}','${escJs(d.original_filename)}')" in HTML
        assert "umResetPw('${u.id}','${escJs(u.email)}')" in HTML


class TestFrontendSseFailClosed:
    def test_no_raw_jwt_token_fallback_in_sse_url(self):
        idx = HTML.find("async function _getSseUrl(")
        body = HTML[idx:idx + 700]
        assert "?token='+encodeURIComponent(_getToken())" not in body, (
            "_getSseUrl must not fall back to embedding the raw JWT in the URL"
        )
        assert "FAIL CLOSED" in body or "fail closed" in body.lower()
