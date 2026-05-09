from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AuditEventSchema(BaseModel):
    id: str
    report_id: Optional[str]
    actor_user_id: Optional[str]
    actor_role: Optional[str]
    action: str
    target_type: Optional[str]
    target_id: Optional[str]
    before: Optional[str]
    after: Optional[str]
    reason: Optional[str]
    extra: Optional[str]
    timestamp: datetime

    model_config = {"from_attributes": True}


class AuditListResponse(BaseModel):
    events: list[AuditEventSchema]
    total: int
    page: int
    page_size: int
