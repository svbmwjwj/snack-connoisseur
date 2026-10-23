"""Stage display for add and rm: a live stage list in a terminal, plain lines otherwise.

A run is a heading and a fixed list of stage titles. ``start`` moves to a stage
(closing the previous one as done), ``end`` closes the run with its outcome.
``LiveStages`` draws the list, an overall bar and a spinner with rich and keeps
the final state on screen; ``PlainStages`` writes one line when a stage starts
and one when it ends, with no control characters.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Literal, Protocol, TextIO

from rich.cells import cell_len
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

Outcome = Literal["done", "failed", "interrupted"]

PLAIN_WORDS: dict[str, str] = {"done": "完成", "failed": "失败", "interrupted": "已中断"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
FRAMES_PER_SECOND = 10
BAR_WIDTH = 30
INDENT = "  "
GAP = "  "


def elapsed(seconds: float) -> str:
    """Format a duration as ``m:ss``, or ``h:mm:ss`` from one hour on."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class StageDisplay(Protocol):
    """What the command session needs from a progress display."""

    def busy(self, text: str) -> None:
        """Show one ongoing activity while no stage list is up."""

    def idle(self) -> None:
        """Remove the activity shown by ``busy``."""

    def begin(self, heading: str, titles: list[str]) -> None:
        """Start a run with all its stage titles."""

    def start(self, number: int, previous: Outcome = "done") -> None:
        """Make stage ``number`` (from 1) current; the previous stage ends with ``previous``."""

    def count(self, done: int, total: int) -> None:
        """Show how far the current stage has counted."""

    def end(self, outcome: Outcome) -> None:
        """End the current stage and the run with ``outcome``."""

    def line(self, text: str) -> None:
        """Print a line that stays on screen."""

    def paused(self) -> AbstractContextManager[None]:
        """Stop drawing while something else (a question, ssh) owns the terminal."""

    def transient(self, text: str) -> AbstractContextManager[None]:
        """Show ``text`` only while the block runs, then clear it from the screen."""


class _Run:
    """Bookkeeping shared by both displays: the current stage and how each one ended."""

    def __init__(self, heading: str, titles: list[str], now: float):
        if not titles:
            raise ValueError("a run needs at least one stage")
        self.heading = heading
        self.titles = list(titles)
        self.began = now
        self.ended: float | None = None
        self.current: int | None = None  # 0-based index of the running stage
        self.since = now
        self.counted: tuple[int, int] | None = None
        self.results: dict[int, tuple[Outcome, float]] = {}

    def label(self, index: int) -> str:
        return f"[{index + 1}/{len(self.titles)}] {self.titles[index]}"

    def start(self, number: int, now: float, previous: Outcome) -> tuple[int, float] | None:
        """Make stage ``number`` current; return the stage this closed and its duration."""
        if not 1 <= number <= len(self.titles):
            raise ValueError(f"stage {number} is outside 1..{len(self.titles)}")
        closed = self.finish(previous, now)
        self.current, self.since = number - 1, now
        return closed

    def finish(self, outcome: Outcome, now: float) -> tuple[int, float] | None:
        """Close the running stage, if any; return it and its duration."""
        if self.current is None:
            return None
        index, took = self.current, now - self.since
        self.results[index] = (outcome, took)
        self.current, self.counted = None, None
        return index, took

    def completed(self) -> int:
        return sum(1 for outcome, _ in self.results.values() if outcome == "done")


class PlainStages(StageDisplay):
    """Line-by-line output for a stdout that is not a terminal."""

    def __init__(self, out: TextIO, *, clock: Callable[[], float] = time.monotonic):
        self._out = out
        self._clock = clock
        self._run: _Run | None = None

    def busy(self, text: str) -> None:
        self._say(f"{text}……")

    def idle(self) -> None:
        pass

    def begin(self, heading: str, titles: list[str]) -> None:
        self._run = _Run(heading, titles, self._clock())
        self._say(heading)

    def start(self, number: int, previous: Outcome = "done") -> None:
        run = _require(self._run)
        self._report(run, run.start(number, self._clock(), previous), previous)
        self._say(run.label(number - 1))

    def count(self, done: int, total: int) -> None:
        pass

    def end(self, outcome: Outcome) -> None:
        if self._run is not None:
            self._report(self._run, self._run.finish(outcome, self._clock()), outcome)
        self._run = None

    def line(self, text: str) -> None:
        self._say(text)

    @contextmanager
    def paused(self) -> Iterator[None]:
        yield

    @contextmanager
    def transient(self, text: str) -> Iterator[None]:
        self._say(text)
        yield

    def _report(self, run: _Run, closed: tuple[int, float] | None, outcome: Outcome) -> None:
        if closed is not None:
            index, took = closed
            self._say(f"{run.label(index)}：{PLAIN_WORDS[outcome]}（{elapsed(took)}）")

    def _say(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()


class LiveStages(StageDisplay):
    """Stage list, overall bar and spinner drawn in place on a terminal.

    The moving block lives in a transient ``rich.live.Live`` that is rebuilt each
    time drawing resumes; ``end`` prints the final block so it stays. A background
    thread redraws it when ``auto_refresh`` is on, so state is guarded by a lock
    that is never held while calling into ``Live``.
    """

    def __init__(
        self,
        console: Console,
        *,
        auto_refresh: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._console = console
        self._auto_refresh = auto_refresh
        self._clock = clock
        self._lock = threading.RLock()
        self._run: _Run | None = None
        self._busy: str | None = None
        self._live: Live | None = None
        self._pauses = 0
        self._announced: set[int] = set()

    def busy(self, text: str) -> None:
        with self._lock:
            self._busy = text
        self._sync()

    def idle(self) -> None:
        with self._lock:
            self._busy = None
        self._sync()

    def begin(self, heading: str, titles: list[str]) -> None:
        with self._lock:
            self._run = _Run(heading, titles, self._clock())
            self._busy = None
            self._announced = set()
        self._console.print(_one_line(heading))
        self._sync()

    def start(self, number: int, previous: Outcome = "done") -> None:
        with self._lock:
            _require(self._run).start(number, self._clock(), previous)
        self.refresh()

    def count(self, done: int, total: int) -> None:
        with self._lock:
            if self._run is not None and self._run.current is not None:
                self._run.counted = (done, total)

    def end(self, outcome: Outcome) -> None:
        with self._lock:
            run, self._run = self._run, None
            if run is None:
                return
            now = self._clock()
            run.finish(outcome, now)
            run.ended = now
            final = self._block(run)
        self._stop_live()
        self._console.print(final)
        self._sync()

    def line(self, text: str) -> None:
        self._console.print(Text(text))

    def refresh(self) -> None:
        """Redraw the block now (tests use this with ``auto_refresh=False``)."""
        live = self._live
        if live is not None:
            live.refresh()

    @contextmanager
    def paused(self) -> Iterator[None]:
        with self._lock:
            self._pauses += 1
            outermost = self._pauses == 1
            note = self._take_note() if outermost else None
        if outermost:
            self._stop_live()
            if note is not None:
                self._console.print(_one_line(note))
        try:
            yield
        finally:
            with self._lock:
                self._pauses -= 1
            self._sync()

    @contextmanager
    def transient(self, text: str) -> Iterator[None]:
        with self.paused(), self._console.screen(hide_cursor=False):
            self._console.print(Text(text))
            yield

    def _take_note(self) -> str | None:
        """The context line for the current stage, the first time it is paused."""
        run = self._run
        if run is None or run.current is None or run.current in self._announced:
            return None
        self._announced.add(run.current)
        return run.label(run.current)

    def _sync(self) -> None:
        """Start or stop the live area to match whether there is anything to draw."""
        with self._lock:
            wanted = self._pauses == 0 and (self._run is not None or self._busy is not None)
        if not wanted:
            self._stop_live()
        elif self._live is None:
            # A fresh Live each time: a stopped one still remembers the height it erased.
            self._live = Live(
                console=self._console,
                auto_refresh=self._auto_refresh,
                refresh_per_second=FRAMES_PER_SECOND,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
                get_renderable=self._render,
            )
            self._live.start(refresh=True)
        else:
            self._live.refresh()

    def _stop_live(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.stop()

    def _render(self) -> Group | Text:
        with self._lock:
            if self._run is not None:
                return self._block(self._run)
            if self._busy is not None:
                return _one_line(f"{self._spinner()} {self._busy}", "cyan")
            return Text()

    def _spinner(self) -> str:
        return SPINNER[int(self._clock() * FRAMES_PER_SECOND) % len(SPINNER)]

    def _block(self, run: _Run) -> Group:
        """The stage list and the overall bar, each row cut to the console width."""
        now = run.ended if run.ended is not None else self._clock()
        rows = [self._row_parts(run, index, now) for index in range(len(run.titles))]
        width = self._console.width
        detail_w = max(cell_len(detail) for _, _, detail, _ in rows)
        time_w = max(cell_len(took) for _, _, _, took in rows)
        fixed = cell_len(INDENT) + 2 + _column(detail_w) + _column(time_w)
        title_w = max(1, min(max(cell_len(t) for t in run.titles), width - fixed))
        lines = [Text()]
        for (marker, style), title, detail, took in rows:
            line = Text(INDENT, no_wrap=True, overflow="ellipsis")
            line.append(marker, style)
            line.append(" ")
            cell = Text(title)
            cell.truncate(title_w, overflow="ellipsis", pad=True)
            line.append_text(cell)
            if detail_w:
                line.append(GAP + " " * (detail_w - cell_len(detail)) + detail)
            if time_w:
                line.append(GAP + took.rjust(time_w), "dim")
            line.rstrip()
            lines.append(line)
        lines += [Text(), self._bar(run, now, width)]
        return Group(*lines)

    def _row_parts(self, run: _Run, index: int, now: float) -> tuple[tuple[str, str], str, str, str]:
        title = run.titles[index]
        if index == run.current:
            counted = f"{run.counted[0]} / {run.counted[1]}" if run.counted else ""
            return (self._spinner(), "cyan"), title, counted, elapsed(now - run.since)
        if index not in run.results:
            return ("·", "dim"), title, "", ""
        outcome, took = run.results[index]
        if outcome == "done":
            return ("✓", "green"), title, "", elapsed(took)
        if outcome == "failed":
            return ("✗", "red"), title, "", elapsed(took)
        return ("✗", "yellow"), title, "已中断", elapsed(took)

    def _bar(self, run: _Run, now: float, width: int) -> Text:
        done, total = run.completed(), len(run.titles)
        tail = f"{GAP}{done}/{total}   已用时 {elapsed(now - run.began)}"
        bar_w = min(BAR_WIDTH, width - cell_len(INDENT) - cell_len(tail))
        line = Text(INDENT, no_wrap=True, overflow="ellipsis")
        if bar_w >= 5:
            filled = round(bar_w * done / total)
            line.append("━" * filled, "green")
            line.append("─" * (bar_w - filled), "dim")
        line.append(tail.lstrip() if bar_w < 5 else tail)
        return line


def terminal_display(stdout: TextIO | None = None) -> StageDisplay:
    """A live display when stdout is a terminal, plain lines otherwise."""
    stream = sys.stdout if stdout is None else stdout
    if stream is not None and _isatty(stream):
        console = Console() if stdout is None else Console(file=stream)
        if console.is_terminal and not console.is_dumb_terminal:
            return LiveStages(console)
    return PlainStages(stream if stream is not None else io.StringIO())


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # no isatty, or the stream is closed
        return False


def _one_line(text: str, style: str = "") -> Text:
    """Text that is cut with an ellipsis instead of wrapping onto a second line."""
    return Text(text, style=style, no_wrap=True, overflow="ellipsis")


def _column(width: int) -> int:
    """Cells an optional right-hand column takes, including its gap."""
    return cell_len(GAP) + width if width else 0


def _require(run: _Run | None) -> _Run:
    if run is None:
        raise RuntimeError("begin() must be called before stages start")
    return run
