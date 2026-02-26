import pytest

from autr.domain.state_machine import PositionState, can_transition, transition


def test_valid_transition_none_to_entering():
    assert can_transition(PositionState.NONE, PositionState.ENTERING)
    assert transition(PositionState.NONE, PositionState.ENTERING) == PositionState.ENTERING


def test_invalid_transition_none_to_open():
    assert not can_transition(PositionState.NONE, PositionState.OPEN)
    with pytest.raises(ValueError):
        transition(PositionState.NONE, PositionState.OPEN)
