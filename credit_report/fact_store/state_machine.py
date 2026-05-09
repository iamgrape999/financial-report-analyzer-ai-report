from __future__ import annotations

from credit_report.fact_store.models import FACT_STATES

# Valid transitions: from_state -> set of allowed to_states
_TRANSITIONS: dict[str, set[str]] = {
    "extracted": {"normalized", "deprecated"},
    "normalized": {"validated", "conflicted", "deprecated"},
    "validated": {"conflicted", "user_overridden", "approved", "deprecated"},
    "conflicted": {"validated", "user_overridden", "approved", "deprecated"},
    "user_overridden": {"approved", "deprecated"},
    "approved": {"deprecated"},
    "deprecated": set(),  # terminal state
}

# States that block report generation export
EXPORT_BLOCKING_STATES = {"conflicted"}

# States that allow use in generation context
GENERATION_ALLOWED_STATES = {"validated", "user_overridden", "approved"}


class InvalidStateTransitionError(ValueError):
    pass


def validate_transition(current_state: str, new_state: str) -> None:
    """Raise InvalidStateTransitionError if the transition is not permitted."""
    allowed = _TRANSITIONS.get(current_state, set())
    if new_state not in allowed:
        raise InvalidStateTransitionError(
            f"Cannot transition fact from '{current_state}' to '{new_state}'. "
            f"Allowed from '{current_state}': {sorted(allowed) or 'none (terminal)'}"
        )


def can_use_for_generation(state: str) -> bool:
    return state in GENERATION_ALLOWED_STATES


def blocks_export(state: str) -> bool:
    return state in EXPORT_BLOCKING_STATES
