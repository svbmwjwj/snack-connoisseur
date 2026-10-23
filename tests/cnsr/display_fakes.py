"""A stage display that records what it was asked to do."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from cnsr.progress import Outcome, StageDisplay


class RecordingDisplay(StageDisplay):
    """Appends ``(method_name, *args)`` to ``calls``; ``paused_now`` is true only inside ``paused()``."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.paused_now = False

    def busy(self, text: str) -> None:
        self.calls.append(("busy", text))

    def idle(self) -> None:
        self.calls.append(("idle",))

    def begin(self, heading: str, titles: list[str]) -> None:
        self.calls.append(("begin", heading, list(titles)))

    def start(self, number: int, previous: Outcome = "done") -> None:
        self.calls.append(("start", number) if previous == "done" else ("start", number, previous))

    def count(self, done: int, total: int) -> None:
        self.calls.append(("count", done, total))

    def end(self, outcome: Outcome) -> None:
        self.calls.append(("end", outcome))

    def line(self, text: str) -> None:
        self.calls.append(("line", text))

    @contextmanager
    def paused(self) -> Iterator[None]:
        self.calls.append(("paused",))
        self.paused_now = True
        try:
            yield
        finally:
            self.paused_now = False
            self.calls.append(("resumed",))

    @contextmanager
    def transient(self, text: str) -> Iterator[None]:
        self.calls.append(("transient", text))
        yield
