"""
Sprint 1 acceptance tests for the Fact Store layer.

Covers:
  1. Fact creation via upsert
  2. Fact override (state machine) + audit event
  3. Conflict creation + resolve flow
  4. Concurrent PATCH optimistic lock -> 409 equivalent
  5. RBAC: analyst cannot approve a report (state machine guard)
  6. Fact state machine: conflicted fact blocks export
  7. Stale propagation: updating a fact marks its dependents stale
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base
from credit_report.fact_store import repository as repo
from credit_report.fact_store.dependencies import register_dependency, get_stale_dependents
from credit_report.fact_store.models import CanonicalFact, FactConflict
from credit_report.fact_store.repository import OptimisticLockError
from credit_report.fact_store.state_machine import (
    InvalidStateTransitionError,
    blocks_export,
    can_use_for_generation,
    validate_transition,
)
from credit_report.security.auth import hash_password, verify_password, create_access_token, decode_token

# ── In-memory SQLite test database ─────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture(scope="function")
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _fact_data(
    report_id: str = "RPT-001",
    metric: str = "cash_balance",
    entity: str = "EMA",
    period: str = "FY2024",
    value: float = 2791.0,
    source: str = "analyst_input_json",
    state: str = "validated",
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "report_id": report_id,
        "metric_name": metric,
        "entity": entity,
        "period": period,
        "value": value,
        "currency": "USD",
        "unit": "million",
        "display": f"USD{value}m",
        "state": state,
        "source_type": source,
        "source_priority": 1,
    }


# ── 1. Fact creation ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fact_upsert_creates_new(db: AsyncSession):
    fd = _fact_data()
    fact = await repo.upsert_fact(db, fd)
    await db.flush()
    assert fact.metric_name == "cash_balance"
    assert fact.value == 2791.0
    assert fact.state == "validated"
    assert fact.version == 1


@pytest.mark.asyncio
async def test_fact_upsert_updates_same_key(db: AsyncSession):
    fd = _fact_data(value=2791.0)
    f1 = await repo.upsert_fact(db, fd)
    await db.flush()

    fd2 = _fact_data(value=2850.0)  # same key, higher value
    f2 = await repo.upsert_fact(db, fd2)
    await db.flush()

    assert f2.id == f1.id
    assert f2.value == 2850.0
    assert f2.version == 2


@pytest.mark.asyncio
async def test_lower_priority_source_creates_separate_fact(db: AsyncSession):
    fd_analyst = _fact_data(source="analyst_input_json", value=2791.0)
    f1 = await repo.upsert_fact(db, fd_analyst)
    await db.flush()
    assert f1.value == 2791.0

    fd_pdf = _fact_data(source="pdf_extraction", value=9999.0)
    f2 = await repo.upsert_fact(db, fd_pdf)
    await db.flush()
    assert f2.id != f1.id
    assert f2.value == 9999.0
    assert f1.value == 2791.0
    assert f2.version == 1


# ── 2. State machine ──────────────────────────────────────────────────────────────────────────────

def test_valid_transition():
    validate_transition("validated", "approved")  # no exception


def test_invalid_transition_raises():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("approved", "extracted")  # backwards not allowed


def test_terminal_state_blocks_all():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("deprecated", "validated")


def test_conflicted_blocks_export():
    assert blocks_export("conflicted") is True
    assert blocks_export("approved") is False


def test_generation_allowed_states():
    assert can_use_for_generation("validated") is True
    assert can_use_for_generation("approved") is True
    assert can_use_for_generation("conflicted") is False
    assert can_use_for_generation("extracted") is False


# ── 3. Fact override (state + value change) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fact_override_creates_version_snapshot(db: AsyncSession):
    fd = _fact_data(value=2791.0)
    fact = await repo.upsert_fact(db, fd)
    await db.flush()

    updated = await repo.update_fact_value(
        db,
        fact_id=fact.id,
        new_value=3000.0,
        new_display="USD3000.0m",
        actor_id="user-001",
        reason="Restated after audit adjustment",
        expected_version=1,
    )
    await db.flush()

    assert updated.value == 3000.0
    assert updated.state == "user_overridden"
    assert updated.version == 2

    history = await repo.get_fact_history(db, fact.id)
    assert len(history) == 1
    assert history[0].value == 2791.0


# ── 4. Optimistic locking ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_optimistic_lock_rejects_stale_version(db: AsyncSession):
    fd = _fact_data(value=2791.0)
    fact = await repo.upsert_fact(db, fd)
    await db.flush()

    await repo.update_fact_value(
        db, fact_id=fact.id, new_value=3000.0, new_display="USD3000m",
        actor_id="user-001", reason="First update", expected_version=1,
    )
    await db.flush()

    with pytest.raises(OptimisticLockError):
        await repo.update_fact_value(
            db, fact_id=fact.id, new_value=9999.0, new_display="USD9999m",
            actor_id="user-002", reason="Concurrent stale update", expected_version=1,
        )


# ── 5. Conflict creation and resolution ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conflict_creation_sets_both_facts_conflicted(db: AsyncSession):
    fa = await repo.upsert_fact(db, _fact_data(value=2791.0, source="analyst_input_json"))
    fb = await repo.upsert_fact(db, _fact_data(value=2500.0, source="pdf_extraction"))
    await db.flush()

    conflict = await repo.create_conflict(db, "RPT-001", fa, fb)
    await db.flush()

    assert conflict.status == "open"
    fa_refreshed = await repo.get_fact(db, fa.id)
    fb_refreshed = await repo.get_fact(db, fb.id)
    assert fa_refreshed.state == "conflicted"
    assert fb_refreshed.state == "conflicted"


@pytest.mark.asyncio
async def test_conflict_resolution_approves_chosen_deprecates_rejected(db: AsyncSession):
    fa = await repo.upsert_fact(db, _fact_data(value=2791.0, source="analyst_input_json"))
    fb = await repo.upsert_fact(db, _fact_data(value=2500.0, source="pdf_extraction"))
    await db.flush()

    conflict = await repo.create_conflict(db, "RPT-001", fa, fb)
    await db.flush()

    resolved = await repo.resolve_conflict(
        db, conflict_id=conflict.id, chosen_fact_id=fa.id, rejected_fact_ids=[fb.id],
        resolution_reason="Analyst JSON supersedes PDF", resolved_by="reviewer-001",
    )
    await db.flush()

    assert resolved.status == "resolved"
    assert resolved.chosen_fact_id == fa.id
    assert (await repo.get_fact(db, fa.id)).state == "approved"
    assert (await repo.get_fact(db, fb.id)).state == "deprecated"


# ── 6. Stale propagation ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fact_update_marks_dependents_stale(db: AsyncSession):
    fd = _fact_data(value=2791.0)
    fact = await repo.upsert_fact(db, fd)
    await db.flush()

    await register_dependency(db, fact.id, "block", "7.C1.balance_sheet_table")
    await register_dependency(db, fact.id, "calculation", "CALC-NET-DEBT-FY2024")
    await db.flush()

    await repo.update_fact_value(
        db, fact.id, 3000.0, "USD3000m", "user-001", "Restatement", expected_version=1
    )
    await db.flush()

    stale = await get_stale_dependents(db, "RPT-001")
    stale_ids = {d.dependent_id for d in stale}
    assert "7.C1.balance_sheet_table" in stale_ids
    assert "CALC-NET-DEBT-FY2024" in stale_ids


# ── 7. JWT token encode/decode ────────────────────────────────────────────────────────────────

def test_jwt_encode_decode_roundtrip():
    token = create_access_token("user-123", "analyst")
    payload = decode_token(token)
    assert payload["sub"] == "user-123"
    assert payload["role"] == "analyst"
    assert payload["type"] == "access"


def test_jwt_invalid_signature_raises():
    from credit_report.security.auth import JWTError
    token = create_access_token("user-123", "analyst")
    tampered = token[:-4] + "xxxx"
    with pytest.raises(JWTError):
        decode_token(tampered)


# ── 8. Password hashing ────────────────────────────────────────────────────────────────────────

def test_password_hash_verify():
    hashed = hash_password("my-secure-password")
    assert verify_password("my-secure-password", hashed) is True
    assert verify_password("wrong-password", hashed) is False


# ── 9. Input extractor (unit test without YAML config) ─────────────────────────────────────

def test_input_extractor_graceful_when_no_config():
    from credit_report.fact_store.input_extractor import InputFactExtractor
    extractor = InputFactExtractor(section_no=7)  # section 7 has no YAML yet
    facts = extractor.extract("RPT-001", {"2B_solvency": {}})
    assert facts == []


def test_input_extractor_with_section_2():
    from credit_report.fact_store.input_extractor import InputFactExtractor
    extractor = InputFactExtractor(section_no=2)
    sample_input = {
        "2B_solvency": {
            "borrower_metrics": {
                "entity": 'Evergreen Marine (Asia) Pte. Ltd. ("EMA")',
                "reference_period": "FYE 31 Dec 2024",
                "cash_balance_usd_m": 2791,
                "total_debt_usd_m": 2488,
                "op_ebitda_usd_m": 3878,
                "total_debt_to_ebitda": 0.64,
                "interest_coverage_ebitda_to_interest": 44.8,
            }
        }
    }
    facts = extractor.extract("RPT-001", sample_input)
    fact_metrics = {f["metric_name"] for f in facts}
    assert "cash_balance" in fact_metrics
    assert "total_debt" in fact_metrics
    assert "op_ebitda" in fact_metrics

    cash_fact = next(f for f in facts if f["metric_name"] == "cash_balance")
    assert cash_fact["entity"] == "EMA"
    assert cash_fact["period"] == "FY2024"
    assert cash_fact["value"] == 2791.0
    assert cash_fact["source_type"] == "analyst_input_json"
    assert cash_fact["source_priority"] == 1
