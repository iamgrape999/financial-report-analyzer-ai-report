from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.fact_store.models import (
    CanonicalFact,
    FactConflict,
    FactDependency,
    FactVersion,
    SOURCE_PRIORITY,
)
from credit_report.fact_store.state_machine import validate_transition

logger = logging.getLogger(__name__)

# Numeric disagreement threshold: values differ by more than 2% of the larger magnitude
_CONFLICT_THRESHOLD_PCT = 0.02

# ── Fact CRUD ──────────────────────────────────────────────────────────────────────────────

async def get_fact(db: AsyncSession, fact_id: str) -> Optional[CanonicalFact]:
    result = await db.execute(select(CanonicalFact).where(CanonicalFact.id == fact_id))
    return result.scalar_one_or_none()


async def get_facts_for_report(
    db: AsyncSession,
    report_id: str,
    state_filter: Optional[str] = None,
) -> list[CanonicalFact]:
    q = select(CanonicalFact).where(CanonicalFact.report_id == report_id)
    if state_filter:
        q = q.where(CanonicalFact.state == state_filter)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_fact_by_key(
    db: AsyncSession,
    report_id: str,
    metric_name: str,
    entity: str,
    period: str,
    source_type: Optional[str] = None,
) -> Optional[CanonicalFact]:
    q = select(CanonicalFact).where(
        CanonicalFact.report_id == report_id,
        CanonicalFact.metric_name == metric_name,
        CanonicalFact.entity == entity,
        CanonicalFact.period == period,
    )
    if source_type:
        q = q.where(CanonicalFact.source_type == source_type)
    else:
        # Return the highest-priority (lowest source_priority number) fact
        q = q.order_by(CanonicalFact.source_priority)
    result = await db.execute(q)
    return result.scalars().first()


async def get_all_facts_by_key(
    db: AsyncSession,
    report_id: str,
    metric_name: str,
    entity: str,
    period: str,
) -> list[CanonicalFact]:
    result = await db.execute(
        select(CanonicalFact).where(
            CanonicalFact.report_id == report_id,
            CanonicalFact.metric_name == metric_name,
            CanonicalFact.entity == entity,
            CanonicalFact.period == period,
        ).order_by(CanonicalFact.source_priority)
    )
    return list(result.scalars().all())


async def upsert_fact(db: AsyncSession, fact_data: dict) -> CanonicalFact:
    """Insert or update a fact by (report_id, metric_name, entity, period, source_type)."""
    incoming_source = fact_data.get("source_type", "analyst_input_json")
    incoming_priority = SOURCE_PRIORITY.get(incoming_source, 99)

    existing = await get_fact_by_key(
        db,
        fact_data["report_id"],
        fact_data["metric_name"],
        fact_data["entity"],
        fact_data["period"],
        source_type=incoming_source,  # match same source only for upsert
    )
    if existing:
        # Update the existing fact from the same source
        incoming_priority = SOURCE_PRIORITY.get(fact_data.get("source_type", "pdf_extraction"), 99)
        if incoming_priority > existing.source_priority:
            # Lower-priority source — skip update, just return existing
            return existing

        # Save version snapshot before update
        old_version = FactVersion(
            id=str(uuid.uuid4()),
            fact_id=existing.id,
            version=existing.version,
            value=existing.value,
            value_text=existing.value_text,
            state=existing.state,
        )
        db.add(old_version)

        existing.value = fact_data.get("value")
        existing.value_text = fact_data.get("value_text")
        existing.currency = fact_data.get("currency")
        existing.unit = fact_data.get("unit")
        existing.display = fact_data.get("display")
        existing.source_type = fact_data.get("source_type", existing.source_type)
        existing.source_priority = incoming_priority
        existing.state = fact_data.get("state", existing.state)
        if fact_data.get("source_evidence_id") is not None:
            existing.source_evidence_id = fact_data["source_evidence_id"]
        existing.version += 1
        return existing
    else:
        fact = CanonicalFact(
            id=fact_data.get("id") or str(uuid.uuid4()),
            report_id=fact_data["report_id"],
            metric_name=fact_data["metric_name"],
            entity=fact_data["entity"],
            period=fact_data["period"],
            value=fact_data.get("value"),
            value_text=fact_data.get("value_text"),
            currency=fact_data.get("currency"),
            unit=fact_data.get("unit"),
            display=fact_data.get("display"),
            state=fact_data.get("state", "extracted"),
            source_type=fact_data.get("source_type", "analyst_input_json"),
            source_priority=SOURCE_PRIORITY.get(fact_data.get("source_type", "analyst_input_json"), 99),
            source_evidence_id=fact_data.get("source_evidence_id"),
            source_section_no=fact_data.get("source_section_no"),
        )
        db.add(fact)
        return fact


async def get_facts_by_document(
    db: AsyncSession, report_id: str, document_id: str
) -> list[CanonicalFact]:
    """Return all canonical facts that were extracted from a specific document."""
    result = await db.execute(
        select(CanonicalFact).where(
            CanonicalFact.report_id == report_id,
            CanonicalFact.source_evidence_id == document_id,
        )
    )
    return list(result.scalars().all())


async def upsert_facts(db: AsyncSession, facts_data: list[dict]) -> list[CanonicalFact]:
    results = []
    for fd in facts_data:
        f = await upsert_fact(db, fd)
        results.append(f)
        # Detect cross-source conflicts (non-blocking — never raises)
        if f.state not in ("conflicted", "deprecated"):
            try:
                await _detect_and_create_conflicts(db, fd["report_id"], f)
            except Exception:
                logger.warning(
                    "conflict detection failed for fact %s: %s",
                    f.id, "see traceback", exc_info=True,
                )
    return results


def _values_disagree(a: CanonicalFact, b: CanonicalFact) -> bool:
    """Return True if two facts have meaningfully different values (> 2% for numeric)."""
    if a.value is not None and b.value is not None:
        denom = max(abs(a.value), abs(b.value))
        if denom == 0:
            return False
        return abs(a.value - b.value) / denom > _CONFLICT_THRESHOLD_PCT
    if a.value_text and b.value_text:
        return a.value_text.strip().lower() != b.value_text.strip().lower()
    return False


async def _detect_and_create_conflicts(
    db: AsyncSession,
    report_id: str,
    new_fact: CanonicalFact,
) -> None:
    """
    After upserting a fact, find existing facts with the same metric key but a
    different source_type whose value disagrees.  Creates a FactConflict row and
    transitions both facts to 'conflicted' if no open conflict already exists for
    the pair.
    """
    peer_result = await db.execute(
        select(CanonicalFact).where(
            CanonicalFact.report_id == report_id,
            CanonicalFact.metric_name == new_fact.metric_name,
            CanonicalFact.entity == new_fact.entity,
            CanonicalFact.period == new_fact.period,
            CanonicalFact.source_type != new_fact.source_type,
            CanonicalFact.state.notin_(["deprecated", "conflicted"]),
        )
    )
    peers = peer_result.scalars().all()

    for peer in peers:
        if not _values_disagree(new_fact, peer):
            continue
        # Check whether an open conflict already exists for this pair
        dup_result = await db.execute(
            select(FactConflict).where(
                FactConflict.report_id == report_id,
                FactConflict.metric_name == new_fact.metric_name,
                FactConflict.status == "open",
                or_(
                    and_(FactConflict.fact_a_id == new_fact.id, FactConflict.fact_b_id == peer.id),
                    and_(FactConflict.fact_a_id == peer.id, FactConflict.fact_b_id == new_fact.id),
                ),
            )
        )
        if dup_result.scalar_one_or_none() is not None:
            continue
        await create_conflict(db, report_id, new_fact, peer)
        logger.info(
            "conflict created: metric=%s entity=%s period=%s src_a=%s src_b=%s",
            new_fact.metric_name, new_fact.entity, new_fact.period,
            new_fact.source_type, peer.source_type,
        )


async def update_fact_state(
    db: AsyncSession,
    fact_id: str,
    new_state: str,
    actor_id: str,
    reason: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> CanonicalFact:
    fact = await get_fact(db, fact_id)
    if not fact:
        raise ValueError(f"Fact {fact_id} not found")

    # Optimistic locking
    if expected_version is not None and fact.version != expected_version:
        raise OptimisticLockError(
            f"Fact {fact_id} version mismatch: expected {expected_version}, got {fact.version}"
        )

    validate_transition(fact.state, new_state)

    # Save version snapshot
    snap = FactVersion(
        id=str(uuid.uuid4()),
        fact_id=fact.id,
        version=fact.version,
        value=fact.value,
        value_text=fact.value_text,
        state=fact.state,
        edited_by=actor_id,
        reason=reason,
    )
    db.add(snap)

    fact.state = new_state
    fact.version += 1
    fact.last_edited_by = actor_id
    if reason:
        fact.override_reason = reason

    # Mark downstream dependents stale
    await _mark_dependents_stale(db, fact_id)

    return fact


async def update_fact_value(
    db: AsyncSession,
    fact_id: str,
    new_value: Optional[float],
    new_display: Optional[str],
    actor_id: str,
    reason: str,
    expected_version: int,
) -> CanonicalFact:
    fact = await get_fact(db, fact_id)
    if not fact:
        raise ValueError(f"Fact {fact_id} not found")

    if fact.version != expected_version:
        raise OptimisticLockError(
            f"Fact {fact_id} version mismatch: expected {expected_version}, got {fact.version}"
        )

    # Save version snapshot
    snap = FactVersion(
        id=str(uuid.uuid4()),
        fact_id=fact.id,
        version=fact.version,
        value=fact.value,
        value_text=fact.value_text,
        state=fact.state,
        edited_by=actor_id,
        reason=reason,
    )
    db.add(snap)

    fact.value = new_value
    fact.display = new_display
    fact.state = "user_overridden"
    fact.version += 1
    fact.last_edited_by = actor_id
    fact.override_reason = reason

    await _mark_dependents_stale(db, fact_id)
    await _mark_calc_results_stale(db, fact.report_id, fact_id)
    await _mark_bound_blocks_stale(db, fact_id)

    return fact


async def _mark_bound_blocks_stale(db: AsyncSession, fact_id: str) -> None:
    """Mark ReportBlock rows stale when a bound fact is overridden."""
    try:
        from credit_report.block_ast.repository import mark_blocks_stale_by_fact
        await mark_blocks_stale_by_fact(db, fact_id)
    except Exception as _e:
        logger.warning("_mark_bound_blocks_stale: failed for fact=%s: %s", fact_id, _e)


async def _mark_dependents_stale(db: AsyncSession, fact_id: str) -> None:
    result = await db.execute(
        select(FactDependency).where(FactDependency.fact_id == fact_id)
    )
    deps = result.scalars().all()
    for dep in deps:
        dep.is_stale = True


async def _mark_calc_results_stale(db: AsyncSession, report_id: str, fact_id: str) -> None:
    """Mark CalculationResult rows stale when one of their input facts changes."""
    import json as _json
    from credit_report.calculation_engine.models import CalculationResult
    result = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == report_id,
            CalculationResult.is_stale == False,  # noqa: E712
        )
    )
    calcs = result.scalars().all()
    for calc in calcs:
        try:
            ids = _json.loads(calc.input_fact_ids or "[]")
        except Exception:
            ids = []
        if fact_id in ids:
            calc.is_stale = True


# ── Fact existence check ──────────────────────────────────────────────────────────────────────

async def fact_exists(db: AsyncSession, report_id: str, fact_id: str) -> bool:
    result = await db.execute(
        select(CanonicalFact.id).where(
            CanonicalFact.report_id == report_id,
            CanonicalFact.id == fact_id,
        )
    )
    return result.scalar_one_or_none() is not None


# ── Conflicts ───────────────────────────────────────────────────────────────────────────────

async def create_conflict(
    db: AsyncSession,
    report_id: str,
    fact_a: CanonicalFact,
    fact_b: CanonicalFact,
) -> FactConflict:
    conflict = FactConflict(
        id=str(uuid.uuid4()),
        report_id=report_id,
        metric_name=fact_a.metric_name,
        entity=fact_a.entity,
        period=fact_a.period,
        fact_a_id=fact_a.id,
        fact_b_id=fact_b.id,
        value_a=fact_a.display or str(fact_a.value),
        value_b=fact_b.display or str(fact_b.value),
        source_a=fact_a.source_type,
        source_b=fact_b.source_type,
        status="open",
    )
    db.add(conflict)

    # Both facts enter conflicted state
    await update_fact_state(db, fact_a.id, "conflicted", "system")
    await update_fact_state(db, fact_b.id, "conflicted", "system")

    return conflict


async def get_open_conflicts(db: AsyncSession, report_id: str) -> list[FactConflict]:
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.report_id == report_id,
            FactConflict.status == "open",
        )
    )
    return list(result.scalars().all())


async def resolve_conflict(
    db: AsyncSession,
    conflict_id: str,
    chosen_fact_id: str,
    rejected_fact_ids: list[str],
    resolution_reason: str,
    resolved_by: str,
) -> FactConflict:
    result = await db.execute(
        select(FactConflict).where(FactConflict.id == conflict_id)
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise ValueError(f"Conflict {conflict_id} not found")

    conflict.status = "resolved"
    conflict.chosen_fact_id = chosen_fact_id
    conflict.resolution_reason = resolution_reason
    conflict.resolved_by = resolved_by
    conflict.resolved_at = datetime.now(timezone.utc)

    # Approve chosen fact, deprecate rejected facts
    await update_fact_state(db, chosen_fact_id, "approved", resolved_by, resolution_reason)
    for rejected_id in rejected_fact_ids:
        await update_fact_state(db, rejected_id, "deprecated", resolved_by, f"Rejected in conflict resolution: {resolution_reason}")

    return conflict


# ── Version history ──────────────────────────────────────────────────────────────────────────

async def get_fact_history(db: AsyncSession, fact_id: str) -> list[FactVersion]:
    result = await db.execute(
        select(FactVersion)
        .where(FactVersion.fact_id == fact_id)
        .order_by(FactVersion.version)
    )
    return list(result.scalars().all())


# ── Optimistic lock ──────────────────────────────────────────────────────────────────────────

class OptimisticLockError(Exception):
    pass
