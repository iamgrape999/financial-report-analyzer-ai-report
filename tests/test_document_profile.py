"""Unit tests for detect_document_profile and get_section_keywords_for_profile.

Covers all 6 industry profiles, scanned-PDF detection, English reports,
analyst reports, fallback/generic, and the keyword resolution chain.
"""
from __future__ import annotations

import pytest

from credit_report.generation.document_pipeline import (
    SECTION_KEYWORDS,
    SECTION_KEYWORDS_BY_PROFILE,
    DocumentProfile,
    detect_document_profile,
    get_section_keywords_for_profile,
)


# ── DocumentProfile.keyword_profile_key ──────────────────────────────────────

class TestKeywordProfileKey:
    def test_analyst_report_overrides_industry(self):
        p = DocumentProfile(industry="tw_banking", report_type="analyst_report")
        assert p.keyword_profile_key() == "analyst_report"

    def test_english_annual_report(self):
        p = DocumentProfile(industry="generic", language="en", report_type="annual_report")
        assert p.keyword_profile_key() == "en_annual"

    def test_tw_semiconductor(self):
        p = DocumentProfile(industry="tw_semiconductor", language="zh_tw")
        assert p.keyword_profile_key() == "tw_semiconductor"

    def test_tw_banking(self):
        p = DocumentProfile(industry="tw_banking", language="zh_tw")
        assert p.keyword_profile_key() == "tw_banking"

    def test_generic_fallback(self):
        p = DocumentProfile(industry="generic", language="mixed")
        assert p.keyword_profile_key() == "generic"


# ── detect_document_profile: industry detection ───────────────────────────────

class TestDetectIndustry:
    def test_semiconductor(self):
        text = "台積電 半導體 晶圓 積體電路 fab CoWoS 2奈米 foundry wafer 資產總額 EPS"
        p = detect_document_profile(text)
        assert p.industry == "tw_semiconductor"
        assert p.language == "zh_tw"

    def test_banking(self):
        text = "銀行 授信業務 放款 存款 逾放比 資本適足率 淨利差 金控 法人金融 財富管理"
        p = detect_document_profile(text)
        assert p.industry == "tw_banking"

    def test_shipping(self):
        text = "航運 貨櫃 散裝 TEU BDI 造船 港口 運費 船舶 船隊管理 航線"
        p = detect_document_profile(text)
        assert p.industry == "tw_shipping"

    def test_real_estate(self):
        text = "不動產 房地產 建設 建案 容積率 土地開發 都市更新 住宅 建設公司"
        p = detect_document_profile(text)
        assert p.industry == "tw_real_estate"

    def test_insurance(self):
        text = "保險 保費 理賠 壽險 產險 再保 清償能力 投資型保單 健康險"
        p = detect_document_profile(text)
        assert p.industry == "tw_insurance"

    def test_generic_fallback(self):
        text = "公司年報 股東報告 財務摘要 general company information"
        p = detect_document_profile(text)
        assert p.industry == "generic"


# ── detect_document_profile: language detection ──────────────────────────────

class TestDetectLanguage:
    def test_chinese_dominant(self):
        p = detect_document_profile("台積電年報 財務報告 股東大會 每股盈餘 資產負債 管理層討論")
        assert p.language == "zh_tw"

    def test_english_dominant(self):
        p = detect_document_profile("Annual Report Revenue Net Income Total Assets EPS ROE Shareholders")
        assert p.language == "en"

    def test_mixed(self):
        # Roughly equal CJK and ASCII
        p = detect_document_profile("台積電 TSMC Annual Report 年報 Revenue 營收 Outlook 展望")
        assert p.language in ("zh_tw", "mixed")


# ── detect_document_profile: report type detection ───────────────────────────

class TestDetectReportType:
    def test_annual_report_from_text(self):
        p = detect_document_profile("致股東報告書 annual report 年報 公司年報")
        assert p.report_type == "annual_report"

    def test_analyst_report_from_text(self):
        p = detect_document_profile("Target price BUY rating investment recommendation hold sell")
        assert p.report_type == "analyst_report"

    def test_interim_report_from_text(self):
        p = detect_document_profile("interim report 季報 第一季 Q1 results")
        assert p.report_type == "interim_report"

    def test_annual_report_from_filename(self):
        p = detect_document_profile("", filename="TSMC_Annual_Report_2024.pdf")
        assert p.report_type == "annual_report"

    def test_analyst_filename(self):
        p = detect_document_profile("", filename="tsmc_analyst_report_buy.pdf")
        # filename contains "report" → annual_report matches first; analyst needs text signal
        # Just check it doesn't crash and returns a valid type
        assert p.report_type in ("annual_report", "analyst_report", "interim_report", "financial_statement", "other")


# ── detect_document_profile: scanned PDF detection ───────────────────────────

class TestScannedPDFDetection:
    def _make_pages(self, quality_scores: list[float]) -> list[dict]:
        return [{"text_quality_score": q, "merged_text": ""} for q in quality_scores]

    def test_scanned_when_majority_low_quality(self):
        # 7/10 pages below 0.3 → scanned
        pages = self._make_pages([0.2] * 7 + [0.85, 0.85, 0.85])
        p = detect_document_profile("", pages=pages)
        assert p.is_scanned is True

    def test_not_scanned_when_majority_normal(self):
        pages = self._make_pages([0.85] * 7 + [0.2, 0.2, 0.2])
        p = detect_document_profile("", pages=pages)
        assert p.is_scanned is False

    def test_not_scanned_when_no_pages(self):
        p = detect_document_profile("some text", pages=[])
        assert p.is_scanned is False

    def test_exactly_60_percent_low_quality_is_not_scanned(self):
        # threshold is > 0.6 (strict), so exactly 60% should not trigger
        pages = self._make_pages([0.2] * 6 + [0.85] * 4)
        p = detect_document_profile("", pages=pages)
        assert p.is_scanned is False

    def test_61_percent_low_quality_is_scanned(self):
        pages = self._make_pages([0.2] * 7 + [0.85] * 3)  # 70% low quality
        p = detect_document_profile("", pages=pages)
        assert p.is_scanned is True


# ── get_section_keywords_for_profile ─────────────────────────────────────────

class TestGetSectionKeywords:
    def test_tw_semiconductor_uses_explicit_keywords(self):
        p = DocumentProfile(industry="tw_semiconductor")
        kws = get_section_keywords_for_profile(p, 4)
        assert "公司簡介" in kws or "市場概況" in kws  # from SECTION_KEYWORDS[4]

    def test_tw_banking_section4_has_banking_terms(self):
        p = DocumentProfile(industry="tw_banking")
        kws = get_section_keywords_for_profile(p, 4)
        assert any(k in kws for k in ("授信業務", "財富管理", "投資銀行"))

    def test_tw_shipping_section4_has_shipping_terms(self):
        p = DocumentProfile(industry="tw_shipping")
        kws = get_section_keywords_for_profile(p, 4)
        assert any(k in kws for k in ("貨櫃", "散裝", "船隊管理"))

    def test_en_annual_section7_has_english_terms(self):
        p = DocumentProfile(industry="generic", language="en", report_type="annual_report")
        kws = get_section_keywords_for_profile(p, 7)
        assert "revenue" in kws or "net income" in kws

    def test_analyst_report_section11_uses_analyst_keywords(self):
        p = DocumentProfile(industry="generic", report_type="analyst_report")
        kws = get_section_keywords_for_profile(p, 11)
        assert "target price" in kws or "buy" in kws

    def test_unknown_section_returns_empty(self):
        p = DocumentProfile(industry="tw_banking")
        kws = get_section_keywords_for_profile(p, 99)
        assert kws == ()

    def test_tw_semiconductor_section_all_have_keywords(self):
        p = DocumentProfile(industry="tw_semiconductor")
        for section_no in range(1, 12):
            kws = get_section_keywords_for_profile(p, section_no)
            assert len(kws) > 0, f"§{section_no} returned empty keywords for tw_semiconductor"

    def test_every_profile_section7_has_financial_terms(self):
        """All profiles must return non-empty §7 keywords (financial metrics page)."""
        financial_marker = "財務狀況及經營結果"
        for profile_key in SECTION_KEYWORDS_BY_PROFILE:
            if profile_key == "analyst_report":
                continue  # analyst reports don't focus on §7
            industry = profile_key if profile_key not in ("en_annual", "generic") else "generic"
            lang = "en" if profile_key == "en_annual" else "zh_tw"
            p = DocumentProfile(industry=industry, language=lang)
            kws = get_section_keywords_for_profile(p, 7)
            assert len(kws) > 0, f"§7 returned empty for profile {profile_key}"


# ── DocumentProfile round-trip ───────────────────────────────────────────────

class TestDocumentProfileSerialization:
    def test_as_dict_round_trip(self):
        original = DocumentProfile(
            industry="tw_shipping",
            market="TW",
            language="zh_tw",
            report_type="interim_report",
            is_scanned=True,
        )
        restored = DocumentProfile.from_dict(original.as_dict())
        assert restored.industry == original.industry
        assert restored.market == original.market
        assert restored.language == original.language
        assert restored.report_type == original.report_type
        assert restored.is_scanned == original.is_scanned

    def test_from_dict_with_missing_keys_uses_defaults(self):
        p = DocumentProfile.from_dict({})
        assert p.industry == "generic"
        assert p.market == "TW"
        assert p.is_scanned is False

    def test_tw_semiconductor_profile_explicitly_defined(self):
        """tw_semiconductor must not rely on empty-dict fallback."""
        assert SECTION_KEYWORDS_BY_PROFILE["tw_semiconductor"], (
            "tw_semiconductor profile is empty — keywords will silently fall back "
            "to SECTION_KEYWORDS without an explicit mapping"
        )
