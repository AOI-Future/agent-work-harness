"""Pure execution-handoff state vocabulary and transition checks."""

from __future__ import annotations

from enum import Enum


class HandoffState(str, Enum):
    PREPARED = "prepared"
    PUBLISHED = "published"
    DELIVERED = "delivered"
    CLAIMED = "claimed"
    RUNNING = "running"
    VERIFIED = "verified"
    COMPLETED = "completed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TRANSITIONS: dict[HandoffState, set[HandoffState]] = {
    HandoffState.PREPARED: {HandoffState.PUBLISHED, HandoffState.CANCELLED},
    HandoffState.PUBLISHED: {HandoffState.DELIVERED, HandoffState.EXPIRED, HandoffState.CANCELLED},
    HandoffState.DELIVERED: {
        HandoffState.CLAIMED,
        HandoffState.REJECTED,
        HandoffState.EXPIRED,
        HandoffState.CANCELLED,
    },
    HandoffState.CLAIMED: {
        HandoffState.RUNNING,
        HandoffState.REJECTED,
        HandoffState.EXPIRED,
        HandoffState.CANCELLED,
    },
    HandoffState.RUNNING: {
        HandoffState.VERIFIED,
        HandoffState.FAILED,
        HandoffState.EXPIRED,
        HandoffState.CANCELLED,
    },
    HandoffState.VERIFIED: {HandoffState.COMPLETED, HandoffState.FAILED, HandoffState.CANCELLED},
    HandoffState.COMPLETED: set(),
    HandoffState.REJECTED: set(),
    HandoffState.EXPIRED: set(),
    HandoffState.FAILED: set(),
    HandoffState.CANCELLED: set(),
}


def can_transition(current: HandoffState | str, next_state: HandoffState | str) -> bool:
    """Return whether a single explicit protocol transition is permitted."""
    try:
        return HandoffState(next_state) in _TRANSITIONS[HandoffState(current)]
    except ValueError:
        return False


def require_transition(current: HandoffState | str, next_state: HandoffState | str) -> None:
    """Reject an invalid state edge without mutating any external state."""
    if not can_transition(current, next_state):
        raise ValueError(f"transition {current!s} -> {next_state!s} is not permitted")
