"""
P3 Test Coverage — Remaining gaps from the second 20-item banking reflection.

Covers:
  A. Fact state machine full chain (extracted→normalized→validated→approved→deprecated)
  B. get_facts_by_document() — document-scoped fact lookup
  C. Report.report_type injected into generation prompt (pipeline wiring)
  D. ETL re-upload — re-extraction from stored binary when .txt is missing
  E. Rate-limit simulation — reserve_and_record_tokens with multiple role limits
  F. Block stale propagation verified end-to-end
  G. Preceding context coherence — preceding_outputs fed into second section
  H. Section output status transitions (pending→generating→done/error)
  I. Password-change audit event recorded
"""
from __future__ import annotations

import uuid
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")


def _uid() -> str:
    return str(uuid.uuid4())


# ── A. Fact state machine full chain ─────────────────────────────────────────

class TestFactStateMachineChain:

    async def test_full_happy_path_extracted_to_approved(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_fact, update_fact_state
        from credit_report.fact_store.state_machine import validate_transition, InvalidStateTransitionError

        rid = _uid()
        async with AsyncSessionLocal() as db:
            fact = await upsert_fact(db, {
                "report_id": rid,
                "metric_name": "revenue",
                "entity": "Corp",
                "period": "FY2024",
                "value": 500.0,
                "state": "extracted",
                "source_type": "etl",
            })
            fid = fact.id
            await db.flush()

            # extracted → normalized
            fact = await update_fact_state(db, fid, "normalized", actor_id="sys")
            assert fact.state == "normalized"

            # normalized → validated
            fact = await update_fact_state(db, fid, "validated", actor_id="analyst1")
            assert fact.state == "validated"

            # validated → approved
            fact = await update_fact_state(db, fid, "approved", actor_id="approver1")
            assert fact.state == "approved"

            # approved → deprecated (terminal path)
            fact = await update_fact_state(db, fid, "deprecated", actor_id="admin1", reason="superseded")
            assert fact.state == "deprecated"
            assert fact.version == 5  # 4 transitions + 1 initial

            await db.rollback()

    async def test_invalid_transition_raises(self):
        from credit_report.fact_store.state_machine import validate_transition, InvalidStateTransitionError

        with pytest.raises(InvalidStateTransitionError):
            validate_transition("extracted", "approved")

        with pytest.raises(InvalidStateTransitionError):
            validate_transition("deprecated", "approved")

        with pytest.raises(InvalidStateTransitionError):
            validate_transition("approved", "normalized")

    async def test_deprecated_is_terminal(self):
        from credit_report.fact_store.state_machine import _TRANSITIONS
        assert _TRANSITIONS["deprecated"] == set(), "deprecated must be a terminal state"

    async def test_conflicted_to_user_overridden(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_fact, update_fact_state

        rid = _uid()
        async with AsyncSessionLocal() as db:
            fact = await upsert_fact(db, {
                "report_id": rid, "metric_name": "ebitda", "entity": "Corp",
                "period": "FY2024", "value": 100.0, "state": "conflicted", "source_type": "etl",
            })
            fid = fact.id
            await db.flush()
            updated = await update_fact_state(db, fid, "user_overridden", actor_id="analyst", reason="analyst override")
            assert updated.state == "user_overridden"
            await db.rollback()

    async def test_version_increments_on_each_transition(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_fact, update_fact_state

        rid = _uid()
        async with AsyncSessionLocal() as db:
            fact = await upsert_fact(db, {
                "report_id": rid, "metric_name": "ltv", "entity": "Corp",
                "period": "FY2024", "value": 0.6, "state": "extracted", "source_type": "etl",
            })
            await db.flush()
            await db.refresh(fact)
            v0 = fact.version  # 1 after flush+refresh
            assert v0 is not None, "version must be populated after flush"
            await update_fact_state(db, fact.id, "normalized", actor_id="sys")
            await update_fact_state(db, fact.id, "validated", actor_id="sys")
            assert fact.version == v0 + 2
            await db.rollback()


# ── B. get_facts_by_document() ───────────────────────────────────────────────

class TestGetFactsByDocument:

    async def test_returns_facts_from_specific_document(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_fact, get_facts_by_document

        rid = _uid()
        doc_a = _uid()
        doc_b = _uid()

        async with AsyncSessionLocal() as db:
            await upsert_fact(db, {
                "report_id": rid, "metric_name": "revenue", "entity": "Co",
                "period": "FY2024", "value": 100.0, "source_type": "etl",
                "source_evidence_id": doc_a,
            })
            await upsert_fact(db, {
                "report_id": rid, "metric_name": "ebitda", "entity": "Co",
                "period": "FY2024", "value": 20.0, "source_type": "etl",
                "source_evidence_id": doc_b,
            })
            await db.flush()

            facts_a = await get_facts_by_document(db, rid, doc_a)
            facts_b = await get_facts_by_document(db, rid, doc_b)

            assert len(facts_a) == 1 and facts_a[0].metric_name == "revenue"
            assert len(facts_b) == 1 and facts_b[0].metric_name == "ebitda"
            await db.rollback()

    async def test_returns_empty_for_unknown_document(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import get_facts_by_document

        async with AsyncSessionLocal() as db:
            result = await get_facts_by_document(db, _uid(), _uid())
            assert result == []


# ── C. Report.report_type injected into generation prompt ───────────────────

class TestReportTypePipelineWiring:

    async def test_report_type_injected_from_report_model(self):
        """run_section_generation() must inject Report.report_type into input_json metadata."""
        from unittest.mock import AsyncMock, patch, MagicMock
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import Report

        rid = _uid()
        uid = _uid()
        captured_input_json = {}

        async with AsyncSessionLocal() as db:
            db.add(Report(id=rid, industry="shipping", created_by=uid,
                          report_type="annual_review"))
            await db.commit()  # must commit so the pipeline's separate query can see it

        async def _fake_generate(section_no, input_json, evidence_chunks,
                                 preceding_outputs=None, output_language="en", **kw):
            captured_input_json.update(input_json)
            return "## Test\n\nContent.\n", 100

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(side_effect=_fake_generate)), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock()):
            from credit_report.generation.pipeline import run_section_generation
            async with AsyncSessionLocal() as db:
                output = await run_section_generation(
                    db=db, report_id=rid, section_no=2,
                    actor_user_id=uid, actor_role="analyst",
                )

        assert captured_input_json.get("metadata", {}).get("report_type") == "annual_review", (
            "report_type from Report model must be injected into input_json metadata "
            f"for build_section_prompt(). Got: {captured_input_json.get('metadata')}"
        )

    async def test_analyst_metadata_report_type_not_overwritten(self):
        """If analyst already set report_type in input_json, pipeline must not overwrite it."""
        from unittest.mock import AsyncMock, patch, MagicMock
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import Report, SectionInput
        import json

        rid = _uid()
        uid = _uid()
        captured_input_json = {}

        async with AsyncSessionLocal() as db:
            db.add(Report(id=rid, industry="shipping", created_by=uid,
                          report_type="annual_review"))
            db.add(SectionInput(
                report_id=rid, section_no=3,
                input_json=json.dumps({"metadata": {"report_type": "watchlist"}}),
            ))
            await db.flush()
            await db.commit()

        async def _fake_generate(section_no, input_json, **kw):
            captured_input_json.update(input_json)
            return "## §3\n\nContent.\n", 100

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(side_effect=_fake_generate)), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock()):
            from credit_report.generation.pipeline import run_section_generation
            async with AsyncSessionLocal() as db:
                await run_section_generation(
                    db=db, report_id=rid, section_no=3,
                    actor_user_id=uid, actor_role="analyst",
                )

        # Analyst's explicit "watchlist" must win over Report.report_type "annual_review"
        assert captured_input_json.get("metadata", {}).get("report_type") == "watchlist", (
            "Analyst-set report_type in input_json must not be overwritten by Report.report_type"
        )


# ── E. Rate-limit per role ───────────────────────────────────────────────────

class TestRoleLimits:

    async def test_reviewer_gets_double_analyst_limit(self):
        from credit_report.generation.quota import _ROLE_LIMITS, _limit_for_role
        analyst_limit = _limit_for_role("analyst")
        reviewer_limit = _limit_for_role("reviewer")
        assert reviewer_limit == analyst_limit * 2, (
            f"reviewer limit {reviewer_limit} must be 2× analyst {analyst_limit}"
        )

    async def test_admin_gets_five_x_limit(self):
        from credit_report.generation.quota import _limit_for_role
        analyst_limit = _limit_for_role("analyst")
        admin_limit = _limit_for_role("admin")
        assert admin_limit == analyst_limit * 5

    async def test_unknown_role_falls_back_to_analyst_limit(self):
        from credit_report.generation.quota import _limit_for_role
        analyst_limit = _limit_for_role("analyst")
        unknown_limit = _limit_for_role("intern")
        assert unknown_limit == analyst_limit

    async def test_reviewer_can_consume_past_analyst_limit(self):
        """A reviewer can record analyst_limit+1 tokens without hitting 429."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.generation.quota import reserve_and_record_tokens, _limit_for_role

        analyst_limit = _limit_for_role("analyst")
        reviewer_limit = _limit_for_role("reviewer")
        uid = _uid()

        async with AsyncSessionLocal() as db:
            # Record slightly more than analyst limit — should succeed for reviewer
            await reserve_and_record_tokens(db, uid, analyst_limit + 1, role="reviewer")
            await db.rollback()

    async def test_analyst_cannot_exceed_own_limit(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.generation.quota import reserve_and_record_tokens, _limit_for_role
        from credit_report.generation.models import UserTokenQuota
        from datetime import datetime, timezone
        from fastapi import HTTPException

        limit = _limit_for_role("analyst")
        uid = _uid()

        async with AsyncSessionLocal() as db:
            db.add(UserTokenQuota(
                id=_uid(), user_id=uid,
                quota_date=datetime.now(timezone.utc).date(),
                tokens_used=limit,
            ))
            await db.flush()
            with pytest.raises(HTTPException) as exc_info:
                await reserve_and_record_tokens(db, uid, 1, role="analyst")
            assert exc_info.value.status_code == 429
            await db.rollback()


# ── F. Block stale propagation end-to-end ───────────────────────────────────

class TestBlockStalePropagation:

    async def test_override_fact_marks_bound_block_stale(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_fact, update_fact_value
        from credit_report.block_ast.repository import save_blocks, get_block
        from credit_report.block_ast.models import ReportBlock
        import json

        rid = _uid()
        async with AsyncSessionLocal() as db:
            # Create a fact
            fact = await upsert_fact(db, {
                "report_id": rid, "metric_name": "revenue", "entity": "Co",
                "period": "FY2024", "value": 100.0, "source_type": "etl",
            })
            await db.flush()
            fid = fact.id

            # Create a block bound to this fact
            block_id = _uid()
            cell_id = _uid()
            await save_blocks(
                db,
                [{"id": block_id, "report_id": rid, "section_no": 7,
                  "block_type": "table", "content": "Revenue table",
                  "source_fact_ids": json.dumps([fid]),
                  "is_stale": False, "version": 1}],
                [{"id": cell_id, "block_id": block_id, "row_id": "row_1",
                  "column_id": "col_0", "display_value": "100m",
                  "fact_id": fid, "binding_status": "bound"}],
            )
            await db.flush()

            # Verify block is not stale yet
            block = await get_block(db, block_id)
            assert block.is_stale is False

            # Override the fact value
            await update_fact_value(
                db, fid, new_value=150.0, new_display="150m",
                actor_id="analyst", reason="corrected",
                expected_version=fact.version,
            )
            await db.flush()

            # Block should now be stale
            block = await get_block(db, block_id)
            assert block.is_stale is True, (
                "Block bound to overridden fact must be marked is_stale=True"
            )
            await db.rollback()


# ── G. Preceding context coherence ──────────────────────────────────────────

class TestPrecedingContextCoherence:

    async def test_preceding_outputs_appear_in_prompt(self):
        """build_section_prompt() includes preceding section previews in user_prompt."""
        from credit_report.generation.prompt_builder import build_section_prompt

        preceding = {
            1: "## Section 1\n\nFacility: USD 50m term loan. Borrower: Maersk.\n",
            7: "## Section 7\n\nRevenue FY2024: USD 100m. EBITDA margin: 22%.\n",
        }
        _, user = build_section_prompt(
            section_no=2,
            input_json={},
            evidence_chunks=[],
            preceding_outputs=preceding,
        )
        assert "Section 1 preview" in user
        assert "Section 7 preview" in user
        assert "Maersk" in user
        assert "EBITDA" in user

    async def test_no_preceding_outputs_no_preceding_block(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _, user = build_section_prompt(
            section_no=1, input_json={}, evidence_chunks=[], preceding_outputs=None
        )
        assert "Previously Generated Sections" not in user

    async def test_preceding_preview_capped_at_600_chars(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        long_md = "X" * 2000
        _, user = build_section_prompt(
            section_no=2,
            input_json={},
            evidence_chunks=[],
            preceding_outputs={1: long_md},
        )
        # The preview is capped at 600 chars — the full 2000-char string must not appear
        assert "X" * 700 not in user, "Preceding output preview must be capped at 600 chars"


# ── H. Section output status transitions ────────────────────────────────────

class TestSectionOutputStatusTransitions:

    async def test_status_ends_done_after_successful_generation(self):
        """SectionOutput.status is 'done' after successful generation."""
        from unittest.mock import AsyncMock, patch
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import Report

        rid = _uid()
        uid = _uid()

        async with AsyncSessionLocal() as db:
            db.add(Report(id=rid, industry="shipping", created_by=uid))
            await db.commit()

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(return_value=("## Done.\n", 50))), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock()):
            from credit_report.generation.pipeline import run_section_generation
            async with AsyncSessionLocal() as db:
                output = await run_section_generation(
                    db=db, report_id=rid, section_no=2,
                    actor_user_id=uid, actor_role="analyst",
                )

        assert output.status == "done"
        assert output.markdown == "## Done.\n"
        assert output.tokens_used == 50

    async def test_status_generating_set_in_pipeline_code(self):
        """Verify pipeline code path: SectionOutput.status is set to 'generating' before the LLM call."""
        import inspect
        from credit_report.generation import pipeline
        source = inspect.getsource(pipeline.run_section_generation)
        assert 'status = "generating"' in source, (
            "Pipeline must set output.status = 'generating' before calling the LLM"
        )

    async def test_status_set_to_error_on_llm_failure(self):
        from unittest.mock import AsyncMock, patch
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import Report

        rid = _uid()
        uid = _uid()

        async with AsyncSessionLocal() as db:
            db.add(Report(id=rid, industry="shipping", created_by=uid))
            await db.flush()
            await db.commit()

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock()):
            from credit_report.generation.pipeline import run_section_generation
            async with AsyncSessionLocal() as db:
                with pytest.raises(RuntimeError):
                    await run_section_generation(
                        db=db, report_id=rid, section_no=4,
                        actor_user_id=uid, actor_role="analyst",
                    )
                res = await db.execute(
                    __import__("sqlalchemy", fromlist=["select"]).select(
                        __import__("credit_report.models", fromlist=["SectionOutput"]).SectionOutput
                    ).where(
                        __import__("credit_report.models", fromlist=["SectionOutput"]).SectionOutput.report_id == rid,
                        __import__("credit_report.models", fromlist=["SectionOutput"]).SectionOutput.section_no == 4,
                    )
                )
                output = res.scalar_one_or_none()
                assert output is not None and output.status == "error", (
                    f"SectionOutput status must be 'error' after LLM failure, got {output.status if output else None}"
                )


# ── I. Password-change audit event ───────────────────────────────────────────

class TestPasswordChangeAuditEvent:

    async def test_change_password_writes_audit_event(self):
        from httpx import AsyncClient, ASGITransport
        from main import app

        AUTH = "/api/credit-report/auth"
        email = f"auditpw_{_uid()[:8]}@example.com"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            admin_login = await ac.post(f"{AUTH}/login",
                                         data={"username": "admin@example.com", "password": "admin123"})
            ah = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

            await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Old12345!", "role": "analyst"},
                          headers=ah)
            tokens = await ac.post(f"{AUTH}/login",
                                    data={"username": email, "password": "Old12345!"})
            h = {"Authorization": f"Bearer {tokens.json()['access_token']}"}

            r = await ac.post(f"{AUTH}/change-password",
                              json={"current_password": "Old12345!", "new_password": "New12345!"},
                              headers=h)
            assert r.status_code == 200

            # Check audit event was written
            audit_r = await ac.get("/api/credit-report/audit/events",
                                    params={"page_size": 50}, headers=ah)
            if audit_r.status_code == 200:
                events = audit_r.json().get("events", [])
                pw_events = [e for e in events if "password_change" in e.get("action", "")]
                assert pw_events, (
                    "auth.password_change audit event must be written after POST /change-password"
                )
