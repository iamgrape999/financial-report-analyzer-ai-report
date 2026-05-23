from __future__ import annotations

from fastapi import APIRouter

from credit_report.api import auth, blocks, calculations, conflicts, export, facts, generate, reports, audit

router = APIRouter(prefix="/api/credit-report")

router.include_router(auth.router)
router.include_router(reports.router)
router.include_router(conflicts.router)
router.include_router(facts.router)
router.include_router(audit.router)
router.include_router(audit.global_router)
router.include_router(calculations.router)
router.include_router(blocks.router)
router.include_router(generate.router)
router.include_router(export.router)
