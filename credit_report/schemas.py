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


class CreateReportRequest(BaseModel):
    industry: str = "marine"
    report_type: Optional[str] = None
    borrower_name: Optional[str] = None
    booking_branch: Optional[str] = None


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
