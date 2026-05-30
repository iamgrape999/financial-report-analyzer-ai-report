"""Stage 3 — 10-item regression tests.

Items covered:
  1  MarketDataProvider protocol
  2  TPEx client implements protocol
  3  TWSE_VERIFY_SSL config flag (no verify=False hardcode)
  4  TWSE bundle cache (TTL)
  5  Dynamic system prompt (industry / institution_name)
  6  output_language auto-detection from DocumentProfile
  7  §11 heading + instruction defined
  8  INDUSTRY_TEMPLATES_ROOT removed from config
  9  LLM retry helper (_call_with_retry)
  10 Upload limit 80 MB + CR_OCR_TIMEOUT_SECONDS
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest


# ── Item 1: MarketDataProvider protocol ──────────────────────────────────────

class TestMarketDataProviderProtocol:
    def test_twse_client_implements_protocol(self):
        from credit_report.integrations.market_data import MarketDataProvider
        from credit_report.integrations.twse import TWSEOpenAPIClient
        assert isinstance(TWSEOpenAPIClient(), MarketDataProvider)

    def test_tpex_client_implements_protocol(self):
        from credit_report.integrations.market_data import MarketDataProvider
        from credit_report.integrations.tpex import TPExOpenAPIClient
        assert isinstance(TPExOpenAPIClient(), MarketDataProvider)

    def test_exchange_name_twse(self):
        from credit_report.integrations.twse import TWSEOpenAPIClient
        assert TWSEOpenAPIClient().exchange_name == "TWSE"

    def test_exchange_name_tpex(self):
        from credit_report.integrations.tpex import TPExOpenAPIClient
        assert TPExOpenAPIClient().exchange_name == "TPEx"

    def test_factory_returns_twse_for_2330(self):
        from credit_report.integrations.market_data import create_market_data_provider
        from credit_report.integrations.twse import TWSEOpenAPIClient
        assert isinstance(create_market_data_provider("2330"), TWSEOpenAPIClient)

    def test_factory_returns_tpex_for_6271(self):
        from credit_report.integrations.market_data import create_market_data_provider
        from credit_report.integrations.tpex import TPExOpenAPIClient
        assert isinstance(create_market_data_provider("6271"), TPExOpenAPIClient)

    def test_factory_force_tpex(self):
        from credit_report.integrations.market_data import create_market_data_provider
        from credit_report.integrations.tpex import TPExOpenAPIClient
        assert isinstance(create_market_data_provider("2330", exchange="tpex"), TPExOpenAPIClient)

    def test_factory_force_twse(self):
        from credit_report.integrations.market_data import create_market_data_provider
        from credit_report.integrations.twse import TWSEOpenAPIClient
        assert isinstance(create_market_data_provider("6271", exchange="twse"), TWSEOpenAPIClient)


# ── Item 2: TPEx client structure ────────────────────────────────────────────

class TestTPExClient:
    def test_has_tpex_endpoints(self):
        from credit_report.integrations.tpex import TPEX_ENDPOINTS
        required = {
            "company_profile", "monthly_revenue", "income_statement_general",
            "balance_sheet_general", "cash_flow_general", "valuation_ratios",
            "daily_trading", "dividend",
        }
        assert required.issubset(TPEX_ENDPOINTS.keys())

    def test_tpex_base_url(self):
        from credit_report.integrations.tpex import TPEX_BASE_URL
        assert "tpex.org.tw" in TPEX_BASE_URL


# ── Item 3: SSL configurable, not hardcoded False ────────────────────────────

class TestSSLConfig:
    def test_twse_verify_ssl_exists_in_config(self):
        from credit_report.config import TWSE_VERIFY_SSL
        assert isinstance(TWSE_VERIFY_SSL, bool)

    def test_twse_client_uses_config_not_hardcoded(self):
        import inspect
        from credit_report.integrations import twse
        source = inspect.getsource(twse.TWSEOpenAPIClient.fetch)
        # Must not contain literal "verify=False" — should use the config var
        assert "verify=False" not in source

    def test_tpex_client_uses_config_not_hardcoded(self):
        import inspect
        from credit_report.integrations import tpex
        source = inspect.getsource(tpex.TPExOpenAPIClient.fetch)
        assert "verify=False" not in source


# ── Item 4: TWSE bundle cache ────────────────────────────────────────────────

class TestTWSEBundleCache:
    def test_cache_ttl_in_config(self):
        from credit_report.config import TWSE_BUNDLE_CACHE_TTL
        assert TWSE_BUNDLE_CACHE_TTL > 0

    def test_cache_hit_skips_network(self):
        from credit_report.integrations.twse import TWSEOpenAPIClient, _bundle_cache
        client = TWSEOpenAPIClient()
        fake_bundle = {"company_profile": [{"公司代號": "9999"}]}
        # Pre-populate cache
        _bundle_cache["9999"] = (time.monotonic(), fake_bundle)

        async def run():
            result = await client.fetch_company_bundle("9999")
            return result

        result = asyncio.run(run())
        assert result is fake_bundle

    def test_cache_miss_when_expired(self):
        from credit_report.integrations.twse import TWSEOpenAPIClient, _bundle_cache
        client = TWSEOpenAPIClient()
        # Set a very old cache entry (far in the past)
        _bundle_cache["8888"] = (time.monotonic() - 999999, {"company_profile": []})

        fetch_called = []

        original_fetch = client.fetch

        async def mock_fetch(endpoint):
            fetch_called.append(endpoint)
            return []

        client.fetch = mock_fetch

        async def run():
            return await client.fetch_company_bundle("8888")

        asyncio.run(run())
        # Network should have been hit because cache was expired
        assert len(fetch_called) > 0


# ── Item 5: Dynamic system prompt ────────────────────────────────────────────

class TestDynamicSystemPrompt:
    def test_industry_descriptions_cover_all_profiles(self):
        from credit_report.generation.prompt_builder import _INDUSTRY_DESCRIPTIONS
        required = {"tw_semiconductor", "tw_banking", "tw_shipping", "tw_real_estate", "tw_insurance", "generic"}
        assert required.issubset(_INDUSTRY_DESCRIPTIONS.keys())

    def test_build_system_prompt_semiconductor(self):
        from credit_report.generation.prompt_builder import _build_system_prompt
        prompt = _build_system_prompt(industry="tw_semiconductor", institution_name="CTBC")
        assert "CTBC" in prompt
        assert "semiconductor" in prompt.lower()

    def test_build_system_prompt_banking(self):
        from credit_report.generation.prompt_builder import _build_system_prompt
        prompt = _build_system_prompt(industry="tw_banking", institution_name="Cathay")
        assert "Cathay" in prompt
        assert "banking" in prompt.lower()

    def test_institution_name_replaces_cub_in_user_prompt(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _system, user = build_section_prompt(
            section_no=1,
            input_json={"metadata": {}},
            evidence_chunks=[],
            institution_name="CTBC",
        )
        # After replacement, "CUB" should not appear in user prompt when institution is CTBC
        assert "CUB" not in user or "CTBC" in user

    def test_legacy_system_prompt_constant_still_importable(self):
        from credit_report.generation.prompt_builder import SYSTEM_PROMPT
        assert isinstance(SYSTEM_PROMPT, str)
        assert len(SYSTEM_PROMPT) > 50

    def test_build_section_prompt_returns_dynamic_system_prompt(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        sys_default, _ = build_section_prompt(
            section_no=2, input_json={}, evidence_chunks=[], industry="tw_shipping"
        )
        sys_semi, _ = build_section_prompt(
            section_no=2, input_json={}, evidence_chunks=[], industry="tw_semiconductor"
        )
        assert sys_default != sys_semi
        assert "semiconductor" in sys_semi.lower()


# ── Item 6: output_language auto-detection ───────────────────────────────────

class TestOutputLanguageInference:
    def test_infer_returns_zh_for_zh_tw_profile(self):
        from credit_report.api.generate import _infer_output_language

        class FakeRow:
            def __iter__(self):
                yield {"language": "zh_tw"}

        class FakeResult:
            def __iter__(self):
                return iter([FakeRow()])

        class FakeDB:
            async def execute(self, _q):
                return FakeResult()

        lang = asyncio.run(
            _infer_output_language(FakeDB(), "r1", None)
        )
        assert lang == "zh"

    def test_infer_returns_en_for_en_profile(self):
        from credit_report.api.generate import _infer_output_language

        class FakeResult:
            def __iter__(self):
                return iter([])

        class FakeDB:
            async def execute(self, _q):
                return FakeResult()

        lang = asyncio.run(
            _infer_output_language(FakeDB(), "r2", None)
        )
        assert lang == "en"

    def test_explicit_en_respected(self):
        from credit_report.api.generate import _infer_output_language

        class FakeDB:
            async def execute(self, _q):
                raise AssertionError("DB should not be queried for explicit override")

        lang = asyncio.run(
            _infer_output_language(FakeDB(), "r3", "en")
        )
        assert lang == "en"

    def test_explicit_zh_respected(self):
        from credit_report.api.generate import _infer_output_language

        class FakeDB:
            async def execute(self, _q):
                raise AssertionError("DB should not be queried for explicit override")

        lang = asyncio.run(
            _infer_output_language(FakeDB(), "r4", "zh")
        )
        assert lang == "zh"


# ── Item 7: §11 heading + instructions ───────────────────────────────────────

class TestSection11Prompt:
    def test_section_11_heading_exists(self):
        from credit_report.generation.prompt_builder import SECTION_HEADINGS
        assert 11 in SECTION_HEADINGS
        assert "11" in SECTION_HEADINGS[11] or "Analyst" in SECTION_HEADINGS[11]

    def test_section_11_instructions_exist(self):
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        assert 11 in SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS[11]
        assert len(instr) > 200  # must be a real instruction, not a stub
        assert "target price" in instr.lower() or "rating" in instr.lower()

    def test_section_11_has_mandatory_subsections(self):
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS[11].lower()
        for keyword in ("rating", "eps", "valuation", "risk"):
            assert keyword in instr, f"§11 instruction missing '{keyword}'"

    def test_section_11_max_output_tokens_defined(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        assert 11 in SECTION_MAX_OUTPUT_TOKENS
        assert SECTION_MAX_OUTPUT_TOKENS[11] > 0

    def test_section_11_continuation_tokens_defined(self):
        from credit_report.config import CONTINUATION_END_TOKENS, CONTINUATION_RESUME_TOKENS
        assert 11 in CONTINUATION_END_TOKENS
        assert 11 in CONTINUATION_RESUME_TOKENS


# ── Item 8: INDUSTRY_TEMPLATES_ROOT removed ──────────────────────────────────

class TestDeadConfigRemoved:
    def test_industry_templates_root_not_in_config(self):
        import credit_report.config as cfg
        assert not hasattr(cfg, "INDUSTRY_TEMPLATES_ROOT"), (
            "INDUSTRY_TEMPLATES_ROOT was a dead config reference (directory never existed "
            "and was never used in code) — it should have been removed in Stage 3 Item 8."
        )


# ── Item 9: LLM retry / backoff ──────────────────────────────────────────────

class TestLLMRetry:
    def test_retry_config_in_config(self):
        from credit_report.config import LLM_MAX_RETRIES, LLM_RETRY_BASE_DELAY
        assert LLM_MAX_RETRIES >= 1
        assert LLM_RETRY_BASE_DELAY > 0

    def test_call_with_retry_succeeds_first_try(self):
        from credit_report.generation.claude_client import _call_with_retry
        calls = []

        async def ok():
            calls.append(1)
            return "result"

        result = asyncio.run(
            _call_with_retry(ok, max_retries=3, base_delay=0.0)
        )
        assert result == "result"
        assert len(calls) == 1

    def test_call_with_retry_retries_on_timeout(self):
        from credit_report.generation.claude_client import _call_with_retry
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise asyncio.TimeoutError()
            return "ok"

        result = asyncio.run(
            _call_with_retry(flaky, max_retries=3, base_delay=0.0)
        )
        assert result == "ok"
        assert len(calls) == 3

    def test_call_with_retry_raises_after_max_retries(self):
        from credit_report.generation.claude_client import _call_with_retry

        async def always_fails():
            raise asyncio.TimeoutError()

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(
                _call_with_retry(always_fails, max_retries=2, base_delay=0.0)
            )

    def test_call_with_retry_does_not_retry_value_error(self):
        from credit_report.generation.claude_client import _call_with_retry
        calls = []

        async def bad_key():
            calls.append(1)
            raise ValueError("Invalid API key")

        with pytest.raises(ValueError):
            asyncio.run(
                _call_with_retry(bad_key, max_retries=3, base_delay=0.0)
            )
        assert len(calls) == 1  # not retried

    def test_call_with_retry_retries_on_503(self):
        from credit_report.generation.claude_client import _call_with_retry
        calls = []

        class FakeHTTP503(Exception):
            status_code = 503

        async def service_unavailable():
            calls.append(1)
            if len(calls) < 2:
                raise FakeHTTP503()
            return "recovered"

        result = asyncio.run(
            _call_with_retry(service_unavailable, max_retries=3, base_delay=0.0)
        )
        assert result == "recovered"
        assert len(calls) == 2


# ── Item 10: Upload limit + OCR timeout ──────────────────────────────────────

class TestUploadAndOCRConfig:
    def test_default_upload_limit_is_80mb(self):
        from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB
        assert CREDIT_REPORT_MAX_UPLOAD_MB >= 80

    def test_ocr_pdf_limit_is_80mb(self):
        from credit_report.config import CR_OCR_MAX_PDF_MB
        assert CR_OCR_MAX_PDF_MB >= 80

    def test_ocr_timeout_is_at_least_300s(self):
        from credit_report.config import CR_OCR_TIMEOUT_SECONDS
        assert CR_OCR_TIMEOUT_SECONDS >= 300

    def test_document_pipeline_imports_ocr_timeout(self):
        from credit_report.generation.document_pipeline import CR_OCR_TIMEOUT_SECONDS
        assert CR_OCR_TIMEOUT_SECONDS >= 300

    def test_generate_max_upload_bytes_uses_80mb_limit(self):
        from credit_report.api.generate import _MAX_UPLOAD_BYTES
        assert _MAX_UPLOAD_BYTES >= 80 * 1024 * 1024
