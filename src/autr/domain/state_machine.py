from enum import Enum


class PositionState(str, Enum):
    NONE = "NONE"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"


_ALLOWED_TRANSITIONS = {
    PositionState.NONE: {PositionState.ENTERING},
    PositionState.ENTERING: {PositionState.OPEN, PositionState.NONE},
    PositionState.OPEN: {PositionState.EXITING},
    PositionState.EXITING: {PositionState.NONE, PositionState.OPEN},
}


def can_transition(current: PositionState, nxt: PositionState) -> bool:
    return nxt in _ALLOWED_TRANSITIONS[current]


def transition(current: PositionState, nxt: PositionState) -> PositionState:
    if not can_transition(current, nxt):
        raise ValueError(f"invalid transition: {current} -> {nxt}")
    return nxt
