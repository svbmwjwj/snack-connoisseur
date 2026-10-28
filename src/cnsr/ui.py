"""Interactive prompts and progress output."""

from __future__ import annotations

import getpass
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, TextIO

from rich.text import Text

from cnsr import runlog
from cnsr.errors import CnsrError
from cnsr.progress import PLAIN_WORDS, Outcome, PlainStages, StageDisplay

if TYPE_CHECKING:  # cnsr.menu loads questionary, which only a terminal session needs
    from cnsr.menu import SelectMany

log = logging.getLogger("cnsr.ui")


class Ui:
    """Prompts and messages for the command-line session.

    Prompts are written by the injected readers (``input`` and ``getpass`` print
    their own prompt text); errors go to ``err`` and everything else to ``out``.
    ``select_fn``, when given, shows the options of ``choose`` as a menu instead of
    a numbered list (see ``cnsr.menu``); ``select_many_fn`` does the same for ``choose_many``.
    When not interactive, any call that needs an answer raises ``CnsrError``
    instead of blocking on a terminal that is not there.

    The stage list is drawn by ``display`` (plain lines on ``out`` by default), which is
    paused for every question and for ``handover``. Everything shown is also written to the
    run log through the ``cnsr.ui`` logger; secrets typed as answers are logged as ``***``.
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        out: TextIO | None = None,
        interactive: bool | None = None,
        err: TextIO | None = None,
        select_fn: Callable[[str, list[str]], int] | None = None,
        display: StageDisplay | None = None,
        select_many_fn: SelectMany | None = None,
    ):
        self._input = input_fn
        self._secret = secret_fn
        self._select = select_fn
        self._select_many = select_many_fn
        # Resolved here rather than in the signature so redirected stdout and stderr are honoured.
        self._out = sys.stdout if out is None else out
        self._err = sys.stderr if err is None else err
        if interactive is None:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        self._interactive = interactive
        self._display: StageDisplay = PlainStages(self._out) if display is None else display
        self._titles: list[str] = []

    def ask(
        self,
        prompt: str,
        default: str | None = None,
        validate: Callable[[str], str | None] | None = None,
        sensitive: bool = False,
    ) -> str:
        """Ask for one line of text.

        Empty input takes ``default``; without a default an answer is required.
        ``validate`` returns an error message to re-ask, or None to accept.
        ``sensitive`` keeps the answer out of the log (it is logged as ``***``).
        """
        label = f"{prompt} [{default}]" if default else prompt
        with self._question(prompt):
            while True:
                value = self._read(self._input, f"{label}：", prompt).strip()
                if sensitive:
                    runlog.secret(value)
                if not value:
                    if default is None:
                        self._show("不能为空，请重新输入。")
                        continue
                    value = default
                    if sensitive:
                        runlog.secret(value)
                error = validate(value) if validate is not None else None
                if error is None:
                    self._answered(value, sensitive)
                    return value
                self._show(error)

    def secret(self, prompt: str) -> str:
        """Ask for a value without echoing it; an answer is required."""
        with self._question(prompt):
            while True:
                value = self._read(self._secret, f"{prompt}：", prompt).strip()
                runlog.secret(value)
                if value:
                    self._answered(value, True)
                    return value
                self._show("不能为空，请重新输入。")

    def choose(self, prompt: str, options: list[str]) -> int:
        """Show the options (as a menu, else numbered) and return the 0-based index of the pick."""
        if not options:
            raise ValueError("options must not be empty")
        with self._question(prompt):
            index = self._pick(prompt, options)
            self._answered(options[index], False)
            return index

    @property
    def interactive(self) -> bool:
        """Whether questions can be answered; when not, every question raises ``CnsrError``."""
        return self._interactive

    def choose_many(
        self,
        prompt: str,
        options: list[str],
        checked: set[int],
        locked: set[int],
        validate: Callable[[list[int]], str | None],
    ) -> list[int]:
        """Tick options (as a checkbox menu, else by numbers) and return the sorted indexes.

        ``checked`` are ticked at first and ``locked`` cannot be unticked. The answer is asked
        again until ``validate`` (given the indexes, locked ones included) returns None.
        """
        if not options:
            raise ValueError("options must not be empty")
        with self._question(prompt):
            if self._select_many is not None:
                try:
                    ticks = self._select_many(prompt, options, checked, locked, validate)
                except EOFError:
                    raise CnsrError(f"缺少参数：{prompt}") from None
            else:
                ticks = self._tick(prompt, options, checked | locked, locked, validate)
            self._answered("、".join(options[index] for index in ticks), False)
            return ticks

    def confirm_text(self, prompt: str, expected: str) -> None:
        """Block until the user types ``expected`` (surrounding spaces ignored)."""
        with self._question(prompt):
            while True:
                typed = self._read(self._input, f"{prompt}：", prompt).strip()
                if typed == expected:
                    self._answered(typed, False)
                    return
                self._show(f"请原样输入“{expected}”。")

    def begin(self, heading: str, titles: list[str]) -> None:
        """Start the stage list of a run."""
        self._titles = list(titles)
        self._display.begin(heading, titles)
        log.info(heading)

    def stage(self, number: int, previous: Outcome = "done") -> None:
        """Move to stage ``number`` (from 1); the previous one ends with ``previous``."""
        self._display.start(number, previous)
        log.info("[%d/%d] %s", number, len(self._titles), self._titles[number - 1])

    def stage_count(self, done: int, total: int) -> None:
        """Show how far the current stage has counted."""
        self._display.count(done, total)

    def end(self, outcome: Outcome) -> None:
        """End the current stage and the run."""
        self._display.end(outcome)
        log.info("结束：%s", PLAIN_WORDS[outcome])

    def busy(self, text: str) -> None:
        self._display.busy(text)

    def idle(self) -> None:
        self._display.idle()

    def transient(self, text: str) -> AbstractContextManager[None]:
        """Show ``text`` only while the block runs."""
        return self._display.transient(text)

    def handover(self) -> AbstractContextManager[None]:
        """Stop drawing while a child process owns the terminal."""
        return self._display.paused()

    def info(self, msg: str) -> None:
        self._display.line(msg)
        log.info("%s", msg)

    def styled(self, text: Text) -> None:
        """Like ``info``, with colours on a terminal; the log gets the plain text."""
        self._display.line(text)
        log.info("%s", text.plain)

    def detail(self, msg: str) -> None:
        """Progress detail for the log only."""
        log.info("%s", msg)

    def warn(self, msg: str) -> None:
        self._display.line(f"警告：{msg}")
        log.warning("%s", msg)

    def error(self, msg: str) -> None:
        """Report what stopped the command, on stderr like ``cnsr.cli`` does."""
        log.error("%s", msg)
        with self._display.paused():
            print(f"错误：{msg}", file=self._err, flush=True)

    def _pick(self, prompt: str, options: list[str]) -> int:
        if self._select is not None:
            try:
                return self._select(prompt, options)
            except EOFError:
                raise CnsrError(f"缺少参数：{prompt}") from None
        self._show(prompt)
        for number, option in enumerate(options, 1):
            self._show(f"{number}. {option}")
        last = len(options)
        while True:
            raw = self._read(self._input, f"请选择 [1-{last}]：", prompt).strip()
            if raw.isdecimal() and 1 <= int(raw) <= last:
                return int(raw) - 1
            self._show(f"请输入 1 到 {last} 之间的数字。")

    def _tick(
        self,
        prompt: str,
        options: list[str],
        default: set[int],
        locked: set[int],
        validate: Callable[[list[int]], str | None],
    ) -> list[int]:
        """The numbered form of ``choose_many``: the numbers typed, or the default on Enter."""
        self._show(prompt)
        for number, option in enumerate(options, 1):
            mark = "（已部署，不能取消）" if number - 1 in locked else ""
            self._show(f"{number}. {option}{mark}")
        last, initial = len(options), ",".join(str(index + 1) for index in sorted(default))
        while True:
            raw = self._read(self._input, f"输入要选的编号，用逗号隔开（直接回车：{initial}）：", prompt).strip()
            parts = [part.strip() for part in raw.replace("，", ",").split(",")] if raw else []
            if not all(part.isdecimal() and 1 <= int(part) <= last for part in parts):
                self._show(f"请输入 1 到 {last} 之间的编号，用逗号隔开。")
                continue
            ticks = sorted({int(part) - 1 for part in parts} | locked) if raw else sorted(default)
            message = validate(ticks)
            if message is None:
                return ticks
            self._show(message)

    @contextmanager
    def _question(self, prompt: str) -> Iterator[None]:
        """Log the question and keep the display paused until it is answered or interrupted."""
        self._require_interactive(prompt)
        log.info("问：%s", prompt)
        with self._display.paused():
            yield

    @staticmethod
    def _answered(value: str, hidden: bool) -> None:
        log.info("答：%s", "***" if hidden else value)

    def _show(self, text: str) -> None:
        """Screen-only line (re-ask hints, menu rows): not worth a log line of its own."""
        self._display.line(text)

    def _require_interactive(self, prompt: str) -> None:
        if not self._interactive:
            raise CnsrError(f"缺少参数：{prompt}")

    @staticmethod
    def _read(reader: Callable[[str], str], text: str, prompt: str) -> str:
        try:
            return reader(text)
        except EOFError:
            # Input closed mid-session (Ctrl-D): same outcome as having no terminal.
            raise CnsrError(f"缺少参数：{prompt}") from None
