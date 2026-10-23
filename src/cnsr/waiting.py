"""Time limits measured on a clock, so the time each attempt itself takes counts toward them."""

from __future__ import annotations

from collections.abc import Callable


class Deadline:
    """``seconds`` from now on ``clock`` (``time.monotonic`` in a real run)."""

    def __init__(self, seconds: float, clock: Callable[[], float]):
        self.seconds = seconds
        self._clock = clock
        self._end = clock() + seconds

    def passed(self) -> bool:
        return self._clock() >= self._end

    def left(self) -> float:
        return max(0.0, self._end - self._clock())

    def step(self, interval: float) -> float:
        """How long to sleep before the next attempt: ``interval``, but never past the deadline."""
        return min(interval, self.left())
