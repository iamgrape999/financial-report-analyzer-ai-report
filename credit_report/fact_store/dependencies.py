from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.fact_store.models import CanonicalFact, FactDependency


async def register_dependency(
    db: AsyncSession,
    fact_id: str,
    dependent_type: str,
    dependent_id: str,
) -> FactDependency:
    """Record that a block/calculation/section depends on a canonical fact."""
    result = await db.execute(
        select(FactDependency).where(
            FactDependency.fact_id == fact_id,
            FactDependency.dependent_type == dependent_type,
            FactDependency.dependent_id == dependent_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.is_stale = False
        return existing

    dep = FactDependency(
        id=str(uuid.uuid4()),
        fact_id=fact_id,
        dependent_type=dependent_type,
        dependent_id=dependent_id,
        is_stale=False,
    )
    db.add(dep)
    return dep


async def get_stale_dependents(
    db: AsyncSession,
    report_id: str,
    dependent_type: str | None = None,
) -> list[FactDependency]:
    """Return all stale dependencies for a report (optionally filtered by type)."""
    # Join through canonical_facts to filter by report_id
    q = (
        select(FactDependency)
        .join(CanonicalFact, CanonicalFact.id == FactDependency.fact_id)
        .where(
            CanonicalFact.report_id == report_id,
            FactDependency.is_stale == True,
        )
    )
    if dependent_type:
        q = q.where(FactDependency.dependent_type == dependent_type)

    result = await db.execute(q)
    return list(result.scalars().all())


async def get_fact_dependencies(
    db: AsyncSession,
    fact_id: str,
) -> list[FactDependency]:
    result = await db.execute(
        select(FactDependency).where(FactDependency.fact_id == fact_id)
    )
    return list(result.scalars().all())


async def clear_stale_flag(db: AsyncSession, dependency_id: str) -> None:
    result = await db.execute(
        select(FactDependency).where(FactDependency.id == dependency_id)
    )
    dep = result.scalar_one_or_none()
    if dep:
        dep.is_stale = False
