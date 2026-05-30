from __future__ import annotations

from credit_report.generation.document_pipeline import (
    is_probably_annual_report,
    validate_annual_report_gates,
    validate_section_proposal_gates,
)


def test_likely_annual_report_filename_is_coerced_to_page_first_type():
    assert is_probably_annual_report(filename="2025 TSMC Annual Report.C.pdf")
    assert is_probably_annual_report(filename="台積電_114年報.pdf")


def test_likely_annual_report_text_is_detected_even_when_upload_type_is_wrong():
    text = """
    台灣積體電路製造股份有限公司 年報
    致股東報告書
    財務概況 6.1
    營業收入淨額 2,894,308
    資產總額 6,518,146
    每股盈餘 45.25
    現金流量 營業活動之淨現金流入
    """
    assert is_probably_annual_report(filename="financial_statement.pdf", text=text)


def test_section7_source_gate_rejects_two_field_false_success_text():
    gate = validate_annual_report_gates("公司名稱 台灣積體電路製造股份有限公司 負責人 魏哲家", section_no=7)

    assert not gate["passed"]
    assert gate["coverage_score"] < 0.8
    assert "revenue" in gate["missing"]
    assert "total_assets" in gate["missing"]


def test_section7_proposal_gate_rejects_identity_only_payload_even_with_good_source_pages():
    source_gate = {"passed": True, "coverage_score": 1.0, "missing": []}
    identity_only_payload = {
        "company_name": "台灣積體電路製造股份有限公司",
        "responsible_person": "魏哲家",
    }

    proposal_gate = validate_section_proposal_gates(7, identity_only_payload, source_gate)

    assert not proposal_gate["passed"]
    assert proposal_gate["coverage_score"] < 0.5
    assert "revenue" in proposal_gate["missing_extracted_metrics"]
    assert "operating_cash_flow" in proposal_gate["missing_extracted_metrics"]


def test_section7_proposal_gate_accepts_financial_statement_payload_with_core_metrics():
    source_gate = {"passed": True, "coverage_score": 1.0, "missing": []}
    financial_payload = {
        "income_statement": {
            "revenue": {"value": 2894308, "unit": "新台幣百萬元"},
            "gross_profit": {"value": 1711111},
            "operating_income": {"value": 1325100},
            "net_income": {"value": 1178070},
            "eps": {"value": 45.25, "unit": "元"},
            "gross_margin": "59.1%",
            "operating_margin": "45.8%",
            "net_margin": "40.7%",
        },
        "balance_sheet": {
            "total_assets": 6518146,
            "total_liabilities": 2388462,
            "total_equity": 4129684,
        },
        "cash_flow": {
            "operating_cash_flow": 1512000,
            "investing_cash_flow": -900000,
            "financing_cash_flow": -400000,
        },
    }

    proposal_gate = validate_section_proposal_gates(7, financial_payload, source_gate)

    assert proposal_gate["passed"]
    assert proposal_gate["coverage_score"] == 1.0
    assert proposal_gate["missing_extracted_metrics"] == []
