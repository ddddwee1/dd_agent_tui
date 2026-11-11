"""Timing for the current conversation, independent of its transcript and UI."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class TimelineSpan:
    kind: str
    round_number: int
    request_number: int
    started_at: float
    label: str = ""
    anchor: str = ""
    ended_at: float | None = None
    status: str = "running"


class TurnTimeline:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.ended_at: float | None = None
        self.status = "running"
        self.round_number = 0
        self.request_number = 0
        self.spans: list[TimelineSpan] = []

    def begin(
        self, kind: str, *, at: float, label: str = "", anchor: str = "",
    ) -> TimelineSpan | None:
        if self.ended_at is not None:
            return None
        span = TimelineSpan(kind, self.round_number, self.request_number, at, label, anchor)
        self.spans.append(span)
        return span

    @staticmethod
    def end(span: TimelineSpan | None, *, at: float, status: str = "done") -> None:
        if span is not None and span.ended_at is None:
            span.ended_at = at
            span.status = status

    def finish(self, status: str) -> None:
        if self.ended_at is None:
            self.ended_at = time.monotonic()
            self.status = status
            for span in self.spans:
                self.end(span, at=self.ended_at, status=status)

    def resume(self) -> None:
        """Continue recording after an idle or interrupted turn."""
        self.ended_at = None
        self.status = "running"

    def elapsed(self, now: float) -> float:
        end = now if self.ended_at is None else self.ended_at
        return end - self.started_at
