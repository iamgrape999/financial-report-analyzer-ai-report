from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.fact_store import repository as repo
from credit_report.fact_store.repository import OptimisticLockError
from credit_report.schemas import (
    FactApproveRequest,
    FactOverrideRequest,
    FactResponse,
    FactStateResponse,
    FactUpdateRequest,
)
from credit_report.security.auth import get_current_user, require_analyst, require_reviewer
from credit_report.security.models import User

router = APIRouter(prefix="/reports/{report_id}/facts", tags=["facts"])


@router.get("", response_model=list[FactResponse])
async def list_facts(
    report_id: str,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await repo.get_facts_for_report(db, report_id, state_filter=state)


@router.get("/conflicts")
async def list_conflicts(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await repo.get_open_conflicts(db, report_id)


@router.get("/{fact_id}", response_model=FactResponse)
async def get_fact(
    report_id: str,
    fact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")
    return fact


@router.get("/{fact_id}/history")
async def get_fact_history(
    report_id: str,
    fact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")
    return await repo.get_fact_history(db, fact_id)


@router.get("/{fact_id}/dependencies")
async def get_fact_dependencies(
    report_id: str,
    fact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from credit_report.fact_store.dependencies import get_fact_dependencies
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")
    return await get_fact_dependencies(db, fact_id)


@router.patch("/{fact_id}", response_model=FactResponse)
async def update_fact_value(
    report_id: str,
    fact_id: str,
    payload: FactUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")

    try:
        updated = await repo.update_fact_value(
            db,
            fact_id=fact_id,
            new_value=payload.value,
            new_display=payload.display,
            actor_id=current_user.id,
            reason=payload.reason,
            expected_version=payload.expected_version,
        )
    except OptimisticLockError as e:
        raise HTTPException(status_code=409, detail=str(e))

    await write_event(
        db,
        action="fact.updated",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="fact",
        target_id=fact_id,
        before=str(fact.value),
        after=str(payload.value),
        reason=payload.reason,
    )
    return updated


@router.post("/{fact_id}/override", response_model=FactStateResponse)
async def override_fact(
    report_id: str,
    fact_id: str,
    payload: FactOverrideRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")

    old_state = fact.state
    try:
        updated = await repo.update_fact_value(
            db,
            fact_id=fact_id,
            new_value=payload.value,
            new_display=payload.display,
            actor_id=current_user.id,
            reason=payload.reason,
            expected_version=payload.expected_version,
        )
    except OptimisticLockError as e:
        raise HTTPException(status_code=409, detail=str(e))

    await write_event(
        db,
        action="fact.override",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="fact",
        target_id=fact_id,
        before=f"value={fact.value} state={old_state}",
        after=f"value={payload.value} state=user_overridden",
        reason=payload.reason,
    )
    return FactStateResponse(
        fact_id=fact_id, old_state=old_state, new_state=updated.state, version=updated.version
    )


@router.post("/{fact_id}/approve", response_model=FactStateResponse)
async def approve_fact(
    report_id: str,
    fact_id: str,
    payload: FactApproveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")

    old_state = fact.state
    try:
        updated = await repo.update_fact_state(
            db,
            fact_id=fact_id,
            new_state="approved",
            actor_id=current_user.id,
            expected_version=payload.expected_version,
        )
    except OptimisticLockError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await write_event(
        db,
        action="fact.approve",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="fact",
        target_id=fact_id,
        before=old_state,
        after="approved",
    )
    return FactStateResponse(
        fact_id=fact_id, old_state=old_state, new_state=updated.state, version=updated.version
    )


@router.post("/{fact_id}/deprecate", response_model=FactStateResponse)
async def deprecate_fact(
    report_id: str,
    fact_id: str,
    reason: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    fact = await repo.get_fact(db, fact_id)
    if not fact or fact.report_id != report_id:
        raise HTTPException(status_code=404, detail="Fact not found")

    old_state = fact.state
    updated = await repo.update_fact_state(
        db, fact_id=fact_id, new_state="deprecated", actor_id=current_user.id, reason=reason
    )

    await write_event(
        db,
        action="fact.deprecate",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="fact",
        target_id=fact_id,
        before=old_state,
        after="deprecated",
        reason=reason,
    )
    return FactStateResponse(
        fact_id=fact_id, old_state=old_state, new_state=updated.state, version=updated.version
    )
