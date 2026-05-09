from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from credit_report.database import Base

# Valid fact states in the state machine
FACT_STATES = (
    "extracted",
    "normalized",
    "validated",
    "conflicted",
    "user_overridden",
    "approved",
    "deprecated",
)

# Source types ordered by priority (lower index = higher trust)
SOURCE_PRIORITY = {
    "analyst_input_json": 1,
    "manual_override": 2,
    "pdf_extraction": 3,
    "calculation": 4,
}


class CanonicalFact(Base):
    __tablename__ = "canonical_facts"
    __table_args__ = (
        UniqueConstraint("report_id", "metric_name", "entity", "period", "source_type", name="uq_fact_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)

    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    display: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    state: Mapped[str] = mapped_column(String(20), nullable=False, default="extracted")
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, default="analyst_input_json")
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_evidence_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    source_section_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_edited_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    override_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    versions: Mapped[list["FactVersion"]] = relationship(
        "FactVersion", back_populates="fact", cascade="all, delete-orphan"
    )
    dependencies: Mapped[list["FactDependency"]] = relationship(
        "FactDependency",
        foreign_keys="FactDependency.fact_id",
        back_populates="fact",
        cascade="all, delete-orphan",
    )

    def fact_key(self) -> tuple[str, str, str]:
        return (self.metric_name, self.entity, self.period)

    def __repr__(self) -> str:
        return f"<Fact {self.metric_name} {self.entity} {self.period} = {self.display or self.value} [{self.state}]>"


class FactVersion(Base):
    __tablename__ = "fact_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(36), ForeignKey("canonical_facts.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    edited_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    fact: Mapped["CanonicalFact"] = relationship("CanonicalFact", back_populates="versions")


class FactConflict(Base):
    __tablename__ = "fact_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)

    fact_a_id: Mapped[str] = mapped_column(String(36), ForeignKey("canonical_facts.id"), nullable=False)
    fact_b_id: Mapped[str] = mapped_column(String(36), ForeignKey("canonical_facts.id"), nullable=False)
    value_a: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    value_b: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_a: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    source_b: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    chosen_fact_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    resolution_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactDependency(Base):
    """Records that a downstream entity (calc/block/section) depends on a fact."""
    __tablename__ = "fact_dependencies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(36), ForeignKey("canonical_facts.id"), nullable=False, index=True)
    dependent_type: Mapped[str] = mapped_column(String(20), nullable=False)  # calculation|block|section
    dependent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    is_stale: Mapped[bool] = mapped_column(nullable=False, default=False)

    fact: Mapped["CanonicalFact"] = relationship(
        "CanonicalFact", foreign_keys=[fact_id], back_populates="dependencies"
    )
