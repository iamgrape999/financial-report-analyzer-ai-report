from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class CellOut(BaseModel):
    row_id: str
    column_id: str
    display_value: Optional[str] = None
    numeric_value: Optional[float] = None
    fact_id: Optional[str] = None
    binding_status: str = "unbound"
    version: int = 1

    model_config = {"from_attributes": True}


class ColumnDef(BaseModel):
    column_id: str
    label: str
    col_type: str = "string"  # string|decimal|integer|percent


class BlockOut(BaseModel):
    id: str
    report_id: str
    section_no: int
    block_type: str
    content: Optional[str] = None
    columns: list[ColumnDef] = []
    cells: list[CellOut] = []
    source_fact_ids: list[str] = []
    validation_status: str
    is_stale: bool
    version: int
    last_edited_by: Optional[str] = None

    model_config = {"from_attributes": True}


class BlockPatchRequest(BaseModel):
    content: str
    reason: Optional[str] = None
    expected_version: int


class CellPatchRequest(BaseModel):
    display_value: str
    numeric_value: Optional[float] = None
    fact_id: Optional[str] = None
