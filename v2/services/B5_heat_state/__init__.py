"""B5 热度状态机"""
from .heat_state import (
    HeatStateMachine, Quadrant, ACTIONS, apply_action, apply_action_batch, _decay_one,
)
