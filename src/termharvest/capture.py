"""Captured frame envelopes shared by live and replay paths."""

from __future__ import annotations

from dataclasses import dataclass

from .screen import ScreenFrame

@dataclass(frozen=True)
class CapturedFrame:
    frame_seq: int
    step_id: str
    expected_screen: str
    frame: ScreenFrame
