"""Pydantic request/response schemas for the Credit Report API."""
from __future__ import annotations

import json as _json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    role: str = "analyst"


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


VALID_INDUSTRIES = {"marine", "shipping", "aviation", "real_estate", "corporate", "finance", "other"}


class CreateReportRequest(BaseModel):
    industry: str = "marine"
    report_type: Optional[str] = None
    borrower_name: Optional[str] = None
    booking_branch: Optional[str] = None

    @field_validator("industry")
    @classmethod
    def validate_industry(cls, v: str) -> str:
        clean = v.strip().lower()
        if clean not in VALID_INDUSTRIES:
            raise ValueError(f"industry must be one of: {sorted(VALID_INDUSTRIES)}")
        return clean


class ReportResponse(BaseModel):
    id: str
    industry: str
    report_type: Optional[str]
    borrower_name: Optional[str]
    booking_branch: Optional[str]
    status: str
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class UpdateReportStatusRequest(BaseModel):
    status: str


class SectionInputPayload(BaseModel):
    section_no: int = Field(..., ge=1, le=11)
    input_json: dict[str, Any]

    @field_validator("input_json")
    @classmethod
    def _check_input_json_size(cls, v: dict) -> dict:
        if len(_json.dumps(v)) > 524_288:  # 512 KB
            raise ValueError("input_json exceeds 512 KB limit")
        return v


class SectionInputResponse(BaseModel):
    section_no: int
    input_json: dict[str, Any]
    saved_at: datetime

    model_config = {"from_attributes": True}


class FactResponse(BaseModel):
    id: str
    report_id: str
    metric_name: str
    entity: str
    period: str
    value: Optional[float]
    value_text: Optional[str]
    currency: Optional[str]
    unit: Optional[str]
    display: Optional[str]
    state: str
    source_type: str
    source_priority: int
    source_section_no: Optional[int]
    version: int
    last_edited_by: Optional[str]
    override_reason: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class FactUpdateRequest(BaseModel):
    value: Optional[float] = None
    display: Optional[str] = None
    reason: str
    expected_version: int


class FactOverrideRequest(BaseModel):
    value: Optional[float] = None
    display: Optional[str] = None
    reason: str
    expected_version: int


class FactApproveRequest(BaseModel):
    expected_version: int


class FactStateResponse(BaseModel):
    fact_id: str
    old_state: str
    new_state: str
    version: int


class ConflictResponse(BaseModel):
    id: str
    report_id: str
    metric_name: str
    entity: str
    period: str
    fact_a_id: str
    fact_b_id: str
    value_a: Optional[str]
    value_b: Optional[str]
    source_a: Optional[str]
    source_b: Optional[str]
    status: str
    chosen_fact_id: Optional[str]
    resolution_reason: Optional[str]
    resolved_by: Optional[str]
    resolved_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class ResolveConflictRequest(BaseModel):
    chosen_fact_id: str
    rejected_fact_ids: list[str]
    resolution_reason: str


class ConflictAISuggestion(BaseModel):
    conflict_id: str
    suggested_winner: str       # "fact_a" | "fact_b" | "uncertain"
    suggested_fact_id: Optional[str]
    confidence: int             # 0–100
    risk_level: str             # "low" | "medium" | "high"
    auto_resolvable: bool       # True only for cross-source (priority rule applies)
    reason: str
    resolution_suggestion: str  # pre-filled text for the resolve call


class AutoResolvePriorityResponse(BaseModel):
    resolved_count: int
    skipped_count: int
    resolved_conflict_ids: list[str]


class FieldSuggestion(BaseModel):
    suggestion_id: str          # deterministic: sha of (report_id, section_no, field_path, fact_id)
    field_path: str             # dot-notation path into input_json
    field_label: str            # human-readable label derived from path
    metric_name: str
    entity: str
    period: str
    current_value: Optional[Any]
    suggested_value: Any
    display: Optional[str]
    currency: Optional[str]
    unit: Optional[str]
    confidence: str             # "high" | "medium" | "low"
    confidence_score: float     # 0–100 numeric
    confidence_reasons: list[str]
    source_type: str
    source_priority: int
    fact_id: str
    fact_state: str
    conflict_warning: Optional[str]
    selectable: bool            # False if conflicted — cannot be batch-selected


class FieldSuggestionsResponse(BaseModel):
    report_id: str
    section_no: int
    total_facts_checked: int
    suggestions: list[FieldSuggestion]


class ApplyFieldSuggestionItem(BaseModel):
    suggestion_id: str
    field_path: str
    suggested_value: Any
    fact_id: str


class ApplySuggestionsRequest(BaseModel):
    items: list[ApplyFieldSuggestionItem]
    apply_mode: str = "only_empty"   # "only_empty" | "overwrite"


class ApplySuggestionsResponse(BaseModel):
    applied_count: int
    skipped_count: int
    conflict_count: int
    applied_paths: list[str]
    skipped_paths: list[str]
    conflict_paths: list[str]


# ── Gap-fill (server-proxied Gemini, no client key) ───────────────────────────

class GapFillRequest(BaseModel):
    company_name: str
    sections: Optional[list[int]] = None  # None = all 1-10


class GapFillSectionResult(BaseModel):
    section_no: int
    filled_count: int
    skipped_count: int


class GapFillResponse(BaseModel):
    company_name: str
    total_filled: int
    sections: list[GapFillSectionResult]
    warning: str = (
        "All gap-fill values are Gemini training-data estimates — "
        "verify every figure against primary sources before approving."
    )
