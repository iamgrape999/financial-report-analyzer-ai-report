"""
Regression tests: finalizePayload() JS key names vs ETL schema contract.

finalizePayload() in static/index.html transforms form data into JSON sent
to the backend.  etl.py expects specific key names (SECTION_EXTRACTION_SCHEMA).
Because _deep_merge_section_input silently merges any key, a key-name mismatch
causes silent data loss rather than an error — the only symptom is missing
fields in the generated report.

These tests run purely on the raw JavaScript source (no browser execution).
They catch regressions where a key is renamed or misspelled.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# ── Load HTML source once ─────────────────────────────────────────────────────

_HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"
_HTML = _HTML_PATH.read_text(encoding="utf-8")


def _extract_finalize_body() -> str:
    """Return the full body of the finalizePayload function."""
    start = _HTML.find("function finalizePayload(secNo,data){")
    assert start != -1, "finalizePayload function not found in index.html"
    end = _HTML.find("\nfunction expandPayload(secNo,data){", start)
    assert end != -1, "expandPayload function not found after finalizePayload"
    return _HTML[start:end]


_FINALIZE_BODY = _extract_finalize_body()


def _extract_section_block(sec_no: int) -> str:
    """Extract the if(secNo===N){...} block from finalizePayload."""
    marker = f"if(secNo==={sec_no}){{"
    start = _FINALIZE_BODY.find(marker)
    if start == -1:
        return ""
    depth = 0
    for i in range(start, len(_FINALIZE_BODY)):
        if _FINALIZE_BODY[i] == "{":
            depth += 1
        elif _FINALIZE_BODY[i] == "}":
            depth -= 1
            if depth == 0:
                return _FINALIZE_BODY[start : i + 1]
    return _FINALIZE_BODY[start:]


_SECTION_BLOCKS = {n: _extract_section_block(n) for n in range(1, 11)}


# ── Sanity ────────────────────────────────────────────────────────────────────


class TestFinalizePayloadExists:
    def test_function_present(self):
        assert "function finalizePayload(secNo,data){" in _HTML, (
            "finalizePayload function must exist in static/index.html"
        )

    @pytest.mark.parametrize("sec_no", range(1, 11))
    def test_section_block_found(self, sec_no):
        assert _SECTION_BLOCKS[sec_no], (
            f"if(secNo==={sec_no}) block not found in finalizePayload — "
            f"section {sec_no} has no data transformation"
        )


# ── §1 deal_comparison_rows ───────────────────────────────────────────────────


class TestSection1DealComparison:
    """ETL expects deal_comparison_rows[{term, proposed_deal, previous_deal}]."""

    src = property(lambda self: _SECTION_BLOCKS[1])

    def test_deal_comparison_rows_key(self):
        assert "deal_comparison_rows" in self.src, (
            "finalizePayload §1 must produce 'deal_comparison_rows' "
            "(matches ETL schema terms_and_conditions.deal_comparison_rows)"
        )

    def test_proposed_deal_key(self):
        assert "proposed_deal" in self.src, (
            "deal_comparison_rows rows must use key 'proposed_deal' — "
            "ETL schema: {term, proposed_deal, previous_deal}"
        )

    def test_previous_deal_key(self):
        assert "previous_deal" in self.src, (
            "deal_comparison_rows rows must use key 'previous_deal' — "
            "ETL schema: {term, proposed_deal, previous_deal}"
        )

    def test_term_key(self):
        assert "term:" in self.src, (
            "deal_comparison_rows rows must include 'term' key"
        )

    def test_input_array_consumed(self):
        """tc.deal_comparison (input) must be read and deleted after transformation."""
        assert "deal_comparison" in self.src, (
            "§1 must read tc.deal_comparison before producing deal_comparison_rows"
        )

    def test_facility_summary_rows_output(self):
        """facility_summary.rows must be an array of structured objects."""
        assert "facility_summary" in self.src, "facility_summary must be in §1 block"
        assert "fs.rows" in self.src, "facility_summary.rows must be transformed"

    def test_borrower_full_name_key(self):
        assert "borrower_full_name" in self.src, (
            "facility_summary row must include 'borrower_full_name' key (ETL schema)"
        )


# ── §2 2E_risk_and_mitigants ──────────────────────────────────────────────────


class TestSection2RiskAndMitigants:
    """ETL expects 2E_risk_and_mitigants.risks[] — NOT risk_factors[]."""

    src = property(lambda self: _SECTION_BLOCKS[2])

    def test_risks_array_key_in_output(self):
        src = self.src
        assert "{risks:" in src or "risks:risks" in src, (
            "finalizePayload §2 must output {risks:[...]} — "
            "ETL schema key is 'risks', not 'risk_factors'"
        )

    def test_additional_risk_factors_key(self):
        assert "additional_risk_factors_from_previous" in self.src, (
            "§2 output must include 'additional_risk_factors_from_previous' key (ETL schema)"
        )

    def test_risk_no_field(self):
        assert "risk_no:" in self.src, (
            "Each risk entry must have 'risk_no' field (ETL schema)"
        )

    def test_risk_level_field(self):
        assert "level:" in self.src, (
            "Each risk entry must have 'level' field (High/Medium/Low)"
        )

    def test_risk_title_field(self):
        assert "title:" in self.src, (
            "Each risk entry must have 'title' field"
        )

    def test_risk_bullets_key(self):
        assert "risk_bullets:" in self.src, (
            "Each risk entry must have 'risk_bullets' key (ETL schema)"
        )

    def test_mitigant_bullets_key(self):
        assert "mitigant_bullets:" in self.src, (
            "Each risk entry must have 'mitigant_bullets' key (ETL schema)"
        )

    def test_2e_section_key_referenced(self):
        assert "2E_risk_and_mitigants" in self.src, (
            "§2 block must reference the '2E_risk_and_mitigants' sub-section key"
        )


# ── §3 external ratings ───────────────────────────────────────────────────────


class TestSection3Ratings:
    """ETL expects entity_abbrev in rating rows; 3C must expose primary_paragraph_verbatim."""

    src = property(lambda self: _SECTION_BLOCKS[3])

    def test_entity_abbrev_in_ratings(self):
        assert "entity_abbrev:" in self.src, (
            "3A_external_ratings rows must use key 'entity_abbrev' (ETL schema) "
            "— not bare 'entity'"
        )

    def test_sp_key_in_ratings(self):
        assert "sp:" in self.src, (
            "Ratings row must have 'sp' key for S&P rating (ETL schema)"
        )

    def test_moodys_key_in_ratings(self):
        assert "moodys:" in self.src, (
            "Ratings row must have 'moodys' key (ETL schema)"
        )

    def test_fitch_key_in_ratings(self):
        assert "fitch:" in self.src, (
            "Ratings row must have 'fitch' key (ETL schema)"
        )

    def test_primary_paragraph_verbatim_synced(self):
        assert "primary_paragraph_verbatim" in self.src, (
            "3C_mas_612 must write 'primary_paragraph_verbatim' (ETL schema key) — "
            "synced from the form field para_1_msr_mapping_verbatim"
        )

    def test_3c_sync_direction(self):
        src = self.src
        assert "primary_paragraph_verbatim=c3.para_1_msr_mapping_verbatim" in src, (
            "3C sync must be: c3.primary_paragraph_verbatim = c3.para_1_msr_mapping_verbatim "
            "(ETL key on the left)"
        )

    def test_3a_section_key(self):
        assert "3A_external_ratings" in self.src, (
            "§3 block must reference '3A_external_ratings'"
        )

    def test_3b_section_key(self):
        assert "3B_internal_ratings" in self.src, (
            "§3 block must reference '3B_internal_ratings'"
        )


# ── §4 management & fleet ─────────────────────────────────────────────────────


class TestSection4ManagementFleet:
    """§4 4C_management must be an array; 4F_fleet must wrap rows in fleet_breakdown."""

    src = property(lambda self: _SECTION_BLOCKS[4])

    def test_4c_management_is_array(self):
        src = self.src
        assert "d['4C_management']=mgArr" in src or "4C_management]=mgArr" in src, (
            "§4 must convert 4C_management object → array (ETL schema: array)"
        )

    def test_4f_fleet_breakdown_key(self):
        assert "fleet_breakdown" in self.src, (
            "§4 4F_fleet must output {fleet_breakdown:[...]} — ETL schema key"
        )

    def test_4c_management_background_field(self):
        assert "background:" in self.src, (
            "4C_management entries must include 'background' field"
        )


# ── §5 security & insurance ───────────────────────────────────────────────────


class TestSection5Security:
    """§5 5A must have security_instruments; 5D must have instruments array."""

    src = property(lambda self: _SECTION_BLOCKS[5])

    def test_5a_security_instruments(self):
        assert "security_instruments" in self.src, (
            "5A_security_overview must output 'security_instruments' array (ETL schema)"
        )

    def test_5a_is_secured_field(self):
        assert "is_secured" in self.src, (
            "5A_security_overview must include 'is_secured' field"
        )

    def test_5d_instruments_key(self):
        assert "instruments:" in self.src, (
            "5D_insurance must output 'instruments' array (ETL schema)"
        )

    def test_5d_applicable_field(self):
        assert "applicable:" in self.src, (
            "5D_insurance must include 'applicable' field"
        )

    def test_5b_refund_guarantee_key(self):
        assert "5B_refund_guarantee" in self.src, (
            "§5 block must handle '5B_refund_guarantee'"
        )

    def test_5f_corporate_guarantee_key(self):
        assert "5F_corporate_guarantee" in self.src, (
            "§5 block must handle '5F_corporate_guarantee'"
        )


# ── §6 milestones ─────────────────────────────────────────────────────────────


class TestSection6Milestones:
    """ETL expects 6D_milestones.commentary_banking_act_33_3 (not banking_act_commentary)."""

    src = property(lambda self: _SECTION_BLOCKS[6])

    def test_commentary_banking_act_key(self):
        assert "commentary_banking_act_33_3" in self.src, (
            "6D_milestones output must have key 'commentary_banking_act_33_3' (ETL schema) — "
            "the form field 'banking_act_commentary' must be renamed on output"
        )

    def test_milestones_array_key(self):
        assert "milestones:" in self.src, (
            "6D_milestones output must have 'milestones' array key"
        )

    def test_milestone_name_field(self):
        assert "milestone:" in self.src, (
            "Each milestone entry must have 'milestone' name field"
        )

    def test_milestone_expected_date_field(self):
        assert "expected_date:" in self.src, (
            "Each milestone entry must have 'expected_date' field (ETL schema)"
        )

    def test_6d_milestones_section_key(self):
        assert "6D_milestones" in self.src, (
            "§6 block must reference '6D_milestones'"
        )


# ── §7 financials ─────────────────────────────────────────────────────────────


class TestSection7Financials:
    """§7 entities_to_analyze must be an array; financials must have income_statement."""

    src = property(lambda self: _SECTION_BLOCKS[7])

    def test_entities_to_analyze_becomes_array(self):
        src = self.src
        assert "d['entities_to_analyze']=arr" in src or "entities_to_analyze]=arr" in src, (
            "§7 must convert entities_to_analyze object → array (ETL schema)"
        )

    def test_income_statement_key(self):
        assert "income_statement" in self.src, (
            "7A_borrower_financials must include 'income_statement' sub-object"
        )

    def test_balance_sheet_key(self):
        assert "balance_sheet" in self.src, (
            "7A_borrower_financials must include 'balance_sheet' sub-object"
        )

    def test_cash_flow_key(self):
        assert "cash_flow" in self.src, (
            "7A_borrower_financials must include 'cash_flow' sub-object"
        )

    def test_7b_key_ratios_key(self):
        assert "7B_key_ratios" in self.src, (
            "§7 block must reference '7B_key_ratios'"
        )

    def test_7c_guarantor_financials_key(self):
        assert "7C_guarantor_financials" in self.src, (
            "§7 block must reference '7C_guarantor_financials'"
        )

    def test_reporting_currency_field(self):
        assert "reporting_currency" in self.src, (
            "7A_borrower_financials must include 'reporting_currency' field"
        )


# ── §9 checklist format ───────────────────────────────────────────────────────


class TestSection9Checklist:
    """ETL expects 4-column format: {category, item, response, remarks}."""

    src = property(lambda self: _SECTION_BLOCKS[9])

    def test_category_key(self):
        assert "category:" in self.src, (
            "§9 checklist items must have 'category' key (4-col ETL format)"
        )

    def test_item_key(self):
        assert "item:" in self.src, (
            "§9 checklist items must have 'item' key"
        )

    def test_response_key(self):
        assert "response:" in self.src, (
            "§9 checklist items must have 'response' key"
        )

    def test_remarks_key(self):
        assert "remarks:" in self.src, (
            "§9 checklist items must have 'remarks' key"
        )

    def test_4col_branch_exists(self):
        """The p.length>=4 branch must handle 4-col input (category|item|response|remarks)."""
        src = self.src
        assert "p.length>=4" in src or "length>=4" in src, (
            "§9 must have 4-column branch: if(p.length>=4) returning {category, item, response, remarks}"
        )

    def test_9a_checklist_key(self):
        assert "9A_checklist" in self.src, (
            "§9 block must reference '9A_checklist'"
        )


# ── §10 projections ───────────────────────────────────────────────────────────


class TestSection10Projections:
    """§10 fleet_growth rows and projection arrays must be structured objects."""

    src = property(lambda self: _SECTION_BLOCKS[10])

    def test_10b_fleet_growth_key(self):
        assert "10B_fleet_growth" in self.src, (
            "§10 block must reference '10B_fleet_growth'"
        )

    def test_10c_projections_key(self):
        assert "10C_projections" in self.src, (
            "§10 block must reference '10C_projections'"
        )

    def test_year_label_field(self):
        assert "year_label:" in self.src, (
            "10B_fleet_growth rows must include 'year_label' field"
        )


# ── Cross-section: all ETL top-level keys reachable ──────────────────────────


class TestAllSectionsHaveEtlKeys:
    """Top-level section keys in finalizePayload output must match ETL schema."""

    # Minimal required keys per section (from SECTION_EXTRACTION_SCHEMA in etl.py)
    ETL_KEYS: dict[int, list[str]] = {
        1: ["facility_summary", "terms_and_conditions"],
        2: ["2A_credit_overview", "2E_risk_and_mitigants"],
        3: ["3A_external_ratings", "3B_internal_ratings", "3C_mas_612"],
        4: ["4C_management", "4F_fleet"],
        5: ["5A_security_overview", "5D_insurance"],
        6: ["6D_milestones"],
        7: ["entities_to_analyze", "7A_borrower_financials", "7B_key_ratios"],
        8: ["8A_acra_banking_charges"],
        9: ["9A_checklist"],
        10: ["10B_fleet_growth", "10C_projections"],
    }

    @pytest.mark.parametrize("sec_no,keys", ETL_KEYS.items())
    def test_section_etl_keys_referenced(self, sec_no, keys):
        src = _SECTION_BLOCKS[sec_no]
        missing = [k for k in keys if k not in src]
        assert not missing, (
            f"§{sec_no} finalizePayload does not reference ETL schema keys: {missing}. "
            f"These keys will not be recognized by etl.py and data will be silently lost."
        )
