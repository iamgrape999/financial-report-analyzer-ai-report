"""
CI/CD tests confirming Complete% and Red Team Check fixes for §1-§10.

Covers:
  1. _extractFacts: snake_case / ALL_CAPS strings are excluded
  2. getCompleteness: expandPayload must be applied before completeness check
  3. redTeamCheck: entity filter excludes internal keys; number thresholds raised to 80/60%
  4. renderQualityReport-level: normData (expanded) is passed to both getCompleteness and redTeamCheck
  5. Section-by-section completeness with fully-populated data
  6. Translations: all Red Team messages contain Chinese (繁體中文) characters
"""
import json
import re
import sys
from pathlib import Path

import pytest

# ── Parse helpers from static/index.html via regex ───────────────────────────

HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text()


def _extract_js_function(name: str) -> str:
    """Return the source text of a top-level JS function."""
    pattern = rf"function {re.escape(name)}\s*\("
    m = re.search(pattern, HTML)
    assert m, f"Function {name!r} not found in index.html"
    start = m.start()
    depth = 0
    i = HTML.index("{", start)
    end = i
    while i < len(HTML):
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    return HTML[start:end]


# ── §1: _extractFacts filters ─────────────────────────────────────────────────


class TestExtractFactsFilter:
    """_extractFacts must exclude snake_case and ALL_CAPS internal strings."""

    def _src(self) -> str:
        return _extract_js_function("_extractFacts")

    def test_underscore_strings_excluded(self):
        src = self._src()
        assert "includes('_')" in src, (
            "_extractFacts must filter strings containing underscores (snake_case keys)"
        )

    def test_allcaps_regex_excluded(self):
        src = self._src()
        # Should have a test for ALL_CAPS tokens like /^[A-Z][A-Z0-9]+$/
        assert "[A-Z][A-Z0-9]" in src, (
            "_extractFacts must filter ALL_CAPS strings via regex"
        )

    def test_array_items_underscore_excluded(self):
        src = self._src()
        # The array-item branch must also check for underscores
        assert "!item.includes('_')" in src, (
            "Array items with underscores must also be excluded"
        )

    def test_placeholder_still_excluded(self):
        src = self._src()
        assert "To be generated" in src

    def test_approve_decline_excluded(self):
        src = self._src()
        assert "APPROVE/DECLINE" in src


# ── §2: redTeamCheck entity filter ────────────────────────────────────────────


class TestEntityFilter:
    """Entity filter must require lowercase chars and block underscore strings."""

    def _src(self) -> str:
        return _extract_js_function("redTeamCheck")

    def test_requires_lowercase_in_entity(self):
        src = self._src()
        assert "/[a-z]/" in src, (
            "Entity filter must require at least one lowercase letter to exclude acronyms"
        )

    def test_no_underscore_in_entity(self):
        src = self._src()
        assert "!s.includes('_')" in src, (
            "Entity filter must reject strings with underscores"
        )

    def test_minimum_length_5(self):
        src = self._src()
        assert "s.length>=5" in src, (
            "Entity filter must enforce minimum length of 5 to avoid short tokens"
        )

    def test_generic_words_excluded(self):
        src = self._src()
        # GENERIC_RE must include common currency/status words
        for word in ("USD", "SGD", "Clear", "Passed", "Yes", "No"):
            assert word in src, f"GENERIC_RE should exclude {word!r}"

    def test_entities_checked_from_larger_sample(self):
        src = self._src()
        # We increased sample from 6 to 8 and threshold from missing>=3 unchanged
        assert "slice(0,8)" in src, (
            "keyNames should sample first 8 candidates for better coverage"
        )


# ── §3: number grounding thresholds ──────────────────────────────────────────


class TestNumberGroundingThresholds:
    """Numeric grounding: danger at >80%, warning at >60% (raised from 50%/25%)."""

    def _src(self) -> str:
        return _extract_js_function("redTeamCheck")

    def test_danger_threshold_80(self):
        src = self._src()
        assert "ungroundedPct>80" in src, (
            "Danger threshold must be >80% (raised from 50%)"
        )

    def test_warning_threshold_60(self):
        src = self._src()
        assert "ungroundedPct>60" in src, (
            "Warning threshold must be >60% (raised from 25%)"
        )

    def test_critNums_min_abs_1(self):
        src = self._src()
        assert "Math.abs(n)>=1" in src, (
            "critNums filter should exclude fractions < 1 (only count integers/significant values)"
        )

    def test_more_format_variants(self):
        src = self._src()
        # Should have absolute value formatting
        assert "abs.toFixed(0)" in src, (
            "Number grounding should check absolute value with toFixed(0)"
        )
        assert "abs.toFixed(1)" in src
        assert "abs.toFixed(2)" in src

    def test_locale_us_format(self):
        src = self._src()
        assert "en-US" in src, (
            "Number grounding should use en-US locale for thousand separators"
        )


# ── §4: renderQualityReport uses expandPayload ────────────────────────────────


class TestRenderQualityReportExpandPayload:
    """renderQualityReport must call expandPayload before completeness and redTeamCheck."""

    def _src(self) -> str:
        return _extract_js_function("renderQualityReport")

    def test_expandPayload_called(self):
        src = self._src()
        assert "expandPayload(n,inputData)" in src, (
            "renderQualityReport must call expandPayload(n, inputData) to normalize FORMAT A → flat paths"
        )

    def test_normData_passed_to_getCompleteness(self):
        src = self._src()
        assert "getCompleteness(n,normData)" in src, (
            "renderQualityReport must pass normData (expanded) to getCompleteness"
        )

    def test_normData_passed_to_redTeamCheck(self):
        src = self._src()
        assert "redTeamCheck(n,normData," in src, (
            "renderQualityReport must pass normData (expanded) to redTeamCheck"
        )


# ── §5: Traditional Chinese translations ─────────────────────────────────────


class TestChineseTranslations:
    """All Red Team and completeness UI messages must be in Traditional Chinese."""

    CHINESE_RE = re.compile(r"[一-鿿]")

    def _has_chinese(self, text: str) -> bool:
        return bool(self.CHINESE_RE.search(text))

    def test_table_header_input_chinese(self):
        assert ">輸入<" in HTML or "輸入</th>" in HTML, (
            "Input column header must be in Chinese: 輸入"
        )

    def test_table_header_completeness_chinese(self):
        assert "完整度" in HTML, "Complete column header must be: 完整度"

    def test_table_header_output_chinese(self):
        assert ">輸出<" in HTML or "輸出</th>" in HTML, (
            "Output column header must be in Chinese: 輸出"
        )

    def test_table_header_redteam_chinese(self):
        assert "紅隊審核" in HTML, "Red Team Check header must be: 紅隊審核"

    def test_section_column_chinese(self):
        assert "段落名稱" in HTML or "段落</th>" in HTML, (
            "Section column header must be Chinese"
        )

    def test_no_analyst_input_msg_chinese(self):
        assert "無分析師輸入資料" in HTML, (
            "No-input danger message must be in Chinese"
        )

    def test_incomplete_input_msg_chinese(self):
        assert "輸入完整度僅" in HTML, (
            "Low completeness warning message must be in Chinese"
        )

    def test_input_ready_not_generated_msg_chinese(self):
        assert "輸入已就緒，但段落尚未生成" in HTML, (
            "Input-ready-but-not-generated message must be in Chinese"
        )

    def test_placeholder_detected_chinese(self):
        assert "輸出中發現未填寫的佔位符" in HTML, (
            "Placeholder detected danger message must be in Chinese"
        )

    def test_lorem_ipsum_chinese(self):
        assert "發現 lorem ipsum 文字" in HTML, (
            "Lorem ipsum message must be in Chinese"
        )

    def test_output_short_chinese(self):
        assert "輸出內容過短" in HTML, "Short output message must be in Chinese"

    def test_sec2_stale_chinese(self):
        assert "§2 綜合評語已過時" in HTML, "§2 stale message must be in Chinese"

    def test_numbers_danger_chinese(self):
        assert "個關鍵數字未出現在輸出中 — 高度幻覺風險" in HTML, (
            "Number grounding danger message must be in Chinese"
        )

    def test_numbers_warning_chinese(self):
        assert "個關鍵數字未出現在輸出中 — 請驗證內容" in HTML, (
            "Number grounding warning message must be in Chinese"
        )

    def test_entity_missing_chinese(self):
        assert "輸入中的關鍵實體未出現在輸出中" in HTML, (
            "Entity missing warning message must be in Chinese"
        )

    def test_status_badge_issues_chinese(self):
        assert "⚠ 有問題" in HTML, "Issues badge must be: ⚠ 有問題"

    def test_status_badge_review_chinese(self):
        assert "⚡ 待審閱" in HTML, "Review badge must be: ⚡ 待審閱"

    def test_status_badge_ok_chinese(self):
        assert "✓ 通過" in HTML, "OK badge must be: ✓ 通過"

    def test_summary_sections_with_input_chinese(self):
        assert "已輸入資料的段落" in HTML, (
            "Sections with Input summary label must be in Chinese"
        )

    def test_summary_sections_generated_chinese(self):
        assert "已生成的段落" in HTML, (
            "Sections Generated summary label must be in Chinese"
        )

    def test_summary_redteam_issues_chinese(self):
        assert "紅隊審核發現的問題" in HTML, (
            "Red Team Issues Found summary label must be in Chinese"
        )

    def test_no_hallucinations_chinese(self):
        assert "未發現幻覺內容" in HTML, (
            "No hallucinations message must be in Chinese"
        )

    def test_requires_correction_chinese(self):
        assert "匯出前需修正" in HTML, (
            "Requires correction before export must be in Chinese"
        )

    def test_alert_danger_chinese(self):
        assert "紅隊審核 — 需要處理" in HTML, (
            "Danger alert header must be in Chinese"
        )

    def test_alert_success_chinese(self):
        assert "紅隊審核通過" in HTML, (
            "Success alert must be in Chinese"
        )

    def test_ready_to_generate_chinese(self):
        assert "✓ 可以生成" in HTML, (
            "Ready-to-generate badge must be in Chinese"
        )

    def test_etl_fields_not_counted_chinese(self):
        assert "ETL欄位不計入" in HTML, (
            "ETL fields not counted note must be in Chinese"
        )

    def test_missing_input_rate_chinese(self):
        assert "欄位缺漏率" in HTML, (
            "Field missing rate label must be in Chinese"
        )

    def test_output_missing_rate_chinese(self):
        assert "輸出缺漏率" in HTML, (
            "Output missing rate label must be in Chinese"
        )


# ── §6: REQUIRED_FIELDS §3 paths after expandPayload ─────────────────────────


class TestSection3RequiredFieldsPaths:
    """§3 REQUIRED_FIELDS must use flat paths that expandPayload produces."""

    def test_borrower_entity_path_present(self):
        assert "3B_internal_ratings.borrower_entity_full_name" in HTML, (
            "§3 REQUIRED_FIELDS must check borrower_entity_full_name (expandPayload output)"
        )

    def test_borrower_fy2024_path_present(self):
        assert "3B_internal_ratings.borrower_fy2024" in HTML, (
            "§3 REQUIRED_FIELDS must check borrower_fy2024 (expandPayload output)"
        )

    def test_mas_612_grade_path_present(self):
        assert "3C_mas_612.grade" in HTML, (
            "§3 REQUIRED_FIELDS must check 3C_mas_612.grade"
        )


# ── §7: Section-by-section completeness thresholds ───────────────────────────


class TestSectionRequiredFieldsDefined:
    """All 10 sections must have at least 2 REQUIRED_FIELDS entries."""

    REQUIRED_FIELDS_BLOCK_RE = re.compile(
        r"const REQUIRED_FIELDS\s*=\s*\{(.+?)\n\};", re.DOTALL
    )

    def _parse_sections(self) -> dict:
        m = self.REQUIRED_FIELDS_BLOCK_RE.search(HTML)
        assert m, "REQUIRED_FIELDS block not found"
        block = m.group(1)
        counts: dict[int, int] = {}
        for sec_no in range(1, 11):
            pattern = rf"\b{sec_no}\s*:\s*\[([^\]]+)\]"
            sm = re.search(pattern, block)
            if sm:
                fields_text = sm.group(1)
                counts[sec_no] = fields_text.count("{p:")
        return counts

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    def test_section_has_required_fields(self, sec_no):
        counts = self._parse_sections()
        count = counts.get(sec_no, 0)
        assert count >= 2, (
            f"§{sec_no} must have at least 2 REQUIRED_FIELDS entries, found {count}"
        )


# ── §8: expandPayload for §3 normalizes rows[] to flat keys ──────────────────


class TestExpandPayloadSection3:
    """expandPayload §3 logic must extract borrower_entity_full_name from rows[]."""

    def _src(self) -> str:
        return _extract_js_function("expandPayload")

    def test_expands_rows_to_borrower_flat(self):
        src = self._src()
        assert "borrower_entity_full_name" in src, (
            "expandPayload must set flat.borrower_entity_full_name from rows"
        )

    def test_expands_rows_to_borrower_fy2024(self):
        src = self._src()
        assert "borrower_fy2024" in src, (
            "expandPayload must set flat.borrower_fy2024 from rows"
        )

    def test_role_borrower_lookup(self):
        src = self._src()
        assert "role==='Borrower'" in src or "role===\"Borrower\"" in src, (
            "expandPayload must find the Borrower row by role field"
        )


# ── §9: No English-only Red Team messages remain ──────────────────────────────


class TestNoEnglishOnlyMessages:
    """Verify no English-only versions of the critical messages remain."""

    def test_old_numbers_message_gone(self):
        assert "key numbers from input not found in output — high hallucination risk" not in HTML, (
            "Old English 'key numbers from input not found in output' message must be removed"
        )

    def test_old_entity_message_gone(self):
        assert "Key entities from input not found in output:" not in HTML, (
            "Old English 'Key entities from input not found in output' message must be removed"
        )

    def test_old_no_input_message_gone(self):
        assert "'No analyst input data — section output will contain hallucinations'" not in HTML, (
            "Old English 'No analyst input data' message must be removed"
        )

    def test_old_placeholder_message_gone(self):
        assert "Unfilled placeholders detected in output — AI failed to generate content" not in HTML, (
            "Old English placeholder message must be removed"
        )

    def test_old_lorem_message_gone(self):
        assert "Lorem ipsum text found — output is placeholder, not real content" not in HTML, (
            "Old English lorem ipsum message must be removed"
        )

    def test_old_sec2_stale_gone(self):
        assert "§2 Overall Comments is stale" not in HTML, (
            "Old English §2 stale message must be removed"
        )

    def test_old_input_complete_message_gone(self):
        assert "Input only " not in HTML or "% complete — draft report may have gaps" not in HTML, (
            "Old English completeness warning message must be removed"
        )

    def test_old_input_ready_not_generated_gone(self):
        assert "'Input ready but section not yet generated'" not in HTML, (
            "Old English 'Input ready but section not yet generated' must be removed"
        )

    def test_old_issues_badge_gone(self):
        # Note: these might appear in comments; check they aren't in message context
        assert "'⚠ Issues'" not in HTML, "Old English Issues badge must be removed"

    def test_old_review_badge_gone(self):
        assert "'⚡ Review'" not in HTML, "Old English Review badge must be removed"

    def test_old_ok_badge_gone(self):
        assert "'✓ OK'" not in HTML, "Old English OK badge must be removed"

    def test_old_sections_with_input_data_gone(self):
        assert "'Sections with Input Data'" not in HTML, (
            "Old English 'Sections with Input Data' label must be removed"
        )

    def test_old_sections_generated_gone(self):
        assert "'Sections Generated'" not in HTML, (
            "Old English 'Sections Generated' label must be removed"
        )

    def test_old_red_team_issues_found_gone(self):
        assert "'Red Team Issues Found'" not in HTML, (
            "Old English 'Red Team Issues Found' label must be removed"
        )

    def test_old_no_hallucinations_gone(self):
        assert "'No hallucinations detected'" not in HTML, (
            "Old English 'No hallucinations detected' must be removed"
        )

    def test_old_requires_correction_gone(self):
        assert "'Requires correction before export'" not in HTML, (
            "Old English 'Requires correction before export' must be removed"
        )

    def test_old_complete_header_gone(self):
        # The table header should now be 完整度, not "Complete"
        # Check that the Complete header is gone in context of the th element
        assert ">Complete<" not in HTML, (
            "Old English 'Complete' column header must be replaced with '完整度'"
        )

    def test_old_red_team_check_header_gone(self):
        assert ">Red Team Check<" not in HTML, (
            "Old English 'Red Team Check' column header must be replaced with '紅隊審核'"
        )

    def test_old_input_header_gone_in_quality_report(self):
        # renderQualityReport JS function must use '輸入', not 'Input'
        # (lines 253-255 use data-i18n attributes and are managed by i18n system — OK)
        src = _extract_js_function("renderQualityReport")
        assert ">Input<" not in src, (
            "renderQualityReport JS must not use old English 'Input' header — use '輸入'"
        )

    def test_old_output_header_gone_in_quality_report(self):
        src = _extract_js_function("renderQualityReport")
        assert ">Output<" not in src, (
            "renderQualityReport JS must not use old English 'Output' header — use '輸出'"
        )

    def test_old_section_header_gone_in_quality_report(self):
        src = _extract_js_function("renderQualityReport")
        assert ">Section<" not in src, (
            "renderQualityReport JS must not use old English 'Section' header — use '段落名稱'"
        )

    def test_old_ready_to_generate_gone(self):
        assert "'✓ Ready to generate'" not in HTML, (
            "Old English 'Ready to generate' badge must be replaced with '✓ 可以生成'"
        )

    def test_old_etl_fields_not_counted_gone(self):
        assert "% filled (ETL fields not counted)" not in HTML, (
            "Old English 'ETL fields not counted' note must be replaced with Chinese"
        )

    def test_old_output_missing_rate_gone(self):
        assert "'Output Missing Rate: '" not in HTML, (
            "Old English 'Output Missing Rate' must be replaced with Chinese"
        )


# ── §10: renderQualityReport not using raw inputData for checks ───────────────


class TestRenderQualityReportNotRawInputData:
    """renderQualityReport must NOT pass raw inputData to getCompleteness or redTeamCheck."""

    def _src(self) -> str:
        return _extract_js_function("renderQualityReport")

    def test_getCompleteness_not_raw(self):
        src = self._src()
        assert "getCompleteness(n,inputData)" not in src, (
            "renderQualityReport must NOT pass raw inputData to getCompleteness — use normData"
        )

    def test_redTeamCheck_not_raw_input(self):
        src = self._src()
        assert "redTeamCheck(n,inputData," not in src, (
            "renderQualityReport must NOT pass raw inputData to redTeamCheck — use normData"
        )
