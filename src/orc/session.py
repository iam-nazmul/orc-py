"""Session lifecycle: state machine, block tracking, operating mode."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class BlockKind(str, Enum):
    Permission = "Permission"
    OrcDecision = "OrcDecision"
    UserInput = "UserInput"


class SessionMode(str, Enum):
    Watch = "Watch"
    Control = "Control"


# ---------------------------------------------------------------------------
# State variants (tagged union via dataclasses)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateRunning:
    name: str = "Running"

    def badge(self) -> str:
        return "◐"


@dataclass(frozen=True)
class StateBlocked:
    name: str = "Blocked"
    kind: BlockKind = BlockKind.UserInput
    reason: str = ""

    def badge(self) -> str:
        if self.kind == BlockKind.Permission:
            return "?"
        return "!"


@dataclass(frozen=True)
class StateAwaitingReview:
    name: str = "AwaitingReview"
    diff_hash: str = ""

    def badge(self) -> str:
        return "◑"


@dataclass(frozen=True)
class StateDone:
    name: str = "Done"
    summary: str = ""

    def badge(self) -> str:
        return "✓"


@dataclass(frozen=True)
class StateFailed:
    name: str = "Failed"
    reason: str = ""

    def badge(self) -> str:
        return "✗"


SessionState = StateRunning | StateBlocked | StateAwaitingReview | StateDone | StateFailed


# ---------------------------------------------------------------------------
# Events that drive state transitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvStarted:
    pass

@dataclass(frozen=True)
class EvPermissionRequested:
    request: str

@dataclass(frozen=True)
class EvPermissionResolved:
    pass

@dataclass(frozen=True)
class EvOrcDecisionNeeded:
    reason: str

@dataclass(frozen=True)
class EvOrcDecisionMade:
    pass

@dataclass(frozen=True)
class EvUserInputNeeded:
    question: str

@dataclass(frozen=True)
class EvUserInputReceived:
    pass

@dataclass(frozen=True)
class EvWorkCompleted:
    diff_hash: str

@dataclass(frozen=True)
class EvReviewSubmitted:
    approved: bool
    feedback: Optional[str] = None

@dataclass(frozen=True)
class EvFinished:
    summary: str

@dataclass(frozen=True)
class EvErrored:
    reason: str

@dataclass(frozen=True)
class EvRestarted:
    pass


SessionEvent = (
    EvStarted | EvPermissionRequested | EvPermissionResolved |
    EvOrcDecisionNeeded | EvOrcDecisionMade |
    EvUserInputNeeded | EvUserInputReceived |
    EvWorkCompleted | EvReviewSubmitted |
    EvFinished | EvErrored | EvRestarted
)


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

class InvalidTransition(Exception):
    pass


def transition(state: SessionState, event: SessionEvent) -> SessionState:
    match (state, event):
        # Running transitions
        case (StateRunning(), EvPermissionRequested(request=r)):
            return StateBlocked(kind=BlockKind.Permission, reason=r)
        case (StateRunning(), EvOrcDecisionNeeded(reason=r)):
            return StateBlocked(kind=BlockKind.OrcDecision, reason=r)
        case (StateRunning(), EvUserInputNeeded(question=q)):
            return StateBlocked(kind=BlockKind.UserInput, reason=q)
        case (StateRunning(), EvWorkCompleted(diff_hash=h)):
            return StateAwaitingReview(diff_hash=h)
        case (StateRunning(), EvFinished(summary=s)):
            return StateDone(summary=s)
        case (StateRunning(), EvErrored(reason=r)):
            return StateFailed(reason=r)

        # Blocked transitions
        case (StateBlocked(), EvPermissionResolved()):
            return StateRunning()
        case (StateBlocked(), EvOrcDecisionMade()):
            return StateRunning()
        case (StateBlocked(), EvUserInputReceived()):
            return StateRunning()
        case (StateBlocked(), EvErrored(reason=r)):
            return StateFailed(reason=r)

        # AwaitingReview transitions
        case (StateAwaitingReview(), EvReviewSubmitted(approved=True)):
            return StateRunning()
        case (StateAwaitingReview(), EvReviewSubmitted(approved=False)):
            return StateRunning()
        case (StateAwaitingReview(), EvErrored(reason=r)):
            return StateFailed(reason=r)

        # Any → Running via restart
        case (_, EvRestarted()):
            return StateRunning()

        # Any → Failed
        case (_, EvErrored(reason=r)):
            return StateFailed(reason=r)

        case _:
            raise InvalidTransition(
                f"No transition from {type(state).__name__} on {type(event).__name__}"
            )


# ---------------------------------------------------------------------------
# Session record
# ---------------------------------------------------------------------------

@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    task: str = ""
    worktree_path: str = ""
    branch: str = ""
    base_commit: str = ""
    state: SessionState = field(default_factory=StateRunning)
    mode: SessionMode = SessionMode.Watch
    model: str = "sonnet"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    claude_session_id: Optional[str] = None
    summary: Optional[str] = None


def state_to_dict(state: SessionState) -> dict:
    """Serialize a SessionState to a dict for DB storage."""
    if isinstance(state, StateRunning):
        return {"name": "Running"}
    elif isinstance(state, StateBlocked):
        return {"name": "Blocked", "kind": state.kind.value, "reason": state.reason}
    elif isinstance(state, StateAwaitingReview):
        return {"name": "AwaitingReview", "diff_hash": state.diff_hash}
    elif isinstance(state, StateDone):
        return {"name": "Done", "summary": state.summary}
    elif isinstance(state, StateFailed):
        return {"name": "Failed", "reason": state.reason}
    raise ValueError(f"Unknown state type: {type(state)}")


def state_from_dict(d: dict) -> SessionState:
    """Deserialize a SessionState from a dict."""
    name = d.get("name", "Running")
    match name:
        case "Running":
            return StateRunning()
        case "Blocked":
            return StateBlocked(
                kind=BlockKind(d.get("kind", "UserInput")),
                reason=d.get("reason", ""),
            )
        case "AwaitingReview":
            return StateAwaitingReview(diff_hash=d.get("diff_hash", ""))
        case "Done":
            return StateDone(summary=d.get("summary", ""))
        case "Failed":
            return StateFailed(reason=d.get("reason", ""))
        case _:
            return StateRunning()
