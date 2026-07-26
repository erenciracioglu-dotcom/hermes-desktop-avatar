"""Avatar state machine.

States:
    idle       — sprite plays a random idle animation; accepts user input.
    thinking   — user has sent a message; waiting for Hermes gateway response.
    talking    — Hermes returned text; TTS (if enabled) may be playing.

Transitions are driven by the controller when the user submits text and
when the gateway responds.
"""
from __future__ import annotations

from enum import Enum


class AvatarState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    TALKING = "talking"

    def is_busy(self) -> bool:
        """Busy states block *new* user submissions."""
        return self in (AvatarState.THINKING, AvatarState.TALKING)
