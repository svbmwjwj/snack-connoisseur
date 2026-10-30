"""Interactive prompts and progress output."""

from __future__ import annotations

import getpass
import logging
import re
import sys
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, TextIO

from rich.text import Text

from cnsr import runlog
from cnsr.errors import CnsrError
from cnsr.progress import PLAIN_WORDS, Outcome, PlainStages, StageDisplay

if TYPE_CHECKING:  # cnsr.menu loads prompt_toolkit, which only a terminal session needs
    from cnsr.menu import SelectMany

log = logging.getLogger("cnsr.ui")
# An option may be columns split by tabs; menus line them up across options with this gap.
COLUMN_GAP = "  "
_QUANTITY = re.compile(r"\d[\d.]* ")
# The answer logged for "back": Esc in a menu, 0 when numbered.
BACK_STEP = "返回"


class GoBack(Exception):
    """The answer "back to the previous question", raised to ``Ui.ask_in_order``."""


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
        select_fn: Callable[..., int] | None = None,
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
        self._asked = 0  # questions shown so far
        self._back = False  # menus offer BACK_STEP (inside ask_in_order, after its first question)
        self._answers: dict[tuple, object] = {}  # each menu's last answer, its default when asked again

    def ask_in_order(self, steps: list[Callable[[], None]]) -> None:
        """Run ``steps`` in order; a menu's "back" runs again the last step before that asked something.

        From the second question on, Esc in every menu (0 when numbered) goes back. A menu asked
        again starts on its last answer. Text questions offer no way back.
        """
        asked: list[int] = []  # the steps that asked a question, in order
        index = 0
        try:
            while index < len(steps):
                before, self._back = self._asked, bool(asked)
                try:
                    steps[index]()
                except GoBack:
                    index = asked.pop()
                    continue
                if self._asked != before:
                    asked.append(index)
                index += 1
        finally:
            self._back = False

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

    def choose(
        self,
        prompt: str,
        options: list[str],
        *,
        default: int | None = None,
        disabled: dict[int, str] | None = None,
    ) -> int:
        """Show the options (as a menu, else numbered) and return the 0-based index of the pick.

        ``default`` is picked at first (on Enter when numbered), unless the same menu was
        answered before; the options in ``disabled`` are shown with the reason they cannot be
        picked. Inside ``ask_in_order``, after its first question, "back" raises ``GoBack``.
        """
        index = self._choose(prompt, options, default, disabled, self._back)
        if index is None:
            raise GoBack
        return index

    def _choose(
        self, prompt: str, options: list[str], default: int | None, disabled: dict[int, str] | None, back: bool
    ) -> int | None:
        """``choose``, where "back" (offered when ``back``) returns None."""
        if not options:
            raise ValueError("options must not be empty")
        key, disabled = (prompt, tuple(options)), disabled or {}
        default = self._answers.get(key, default)
        if default in disabled:  # Enter takes the next option that can be picked
            default = next((i for i in [*range(default, len(options)), *range(default)] if i not in disabled), None)
        with self._question(prompt):
            index = self._pick(prompt, options, default, disabled, back)
            if index is None:
                self._answered(BACK_STEP, False)
                return None
            self._answered(plain(options[index]), False)
            self._answers[key] = index
            return index

    def choose_or_more(
        self,
        prompt: str,
        featured: list[str],
        more: str,
        every: list[str],
        *,
        default: int = 0,
        disabled: dict[int, str] | None = None,
    ) -> tuple[bool, int]:
        """Show ``featured`` and ``more``; ``more`` opens ``every``, where "back" shows the first again.

        Returns whether the pick is one of ``featured``, and its index there or in ``every``.
        ``default`` and ``disabled`` are those of the first menu, shown again after going back.
        """
        while True:
            index = self.choose(prompt, [*featured, more], default=default, disabled=disabled)
            if index < len(featured):
                return True, index
            index = self.choose_back(prompt, every)
            if index is not None:
                return False, index

    def choose_back(
        self, prompt: str, options: list[str], *, default: int = 0, disabled: dict[int, str] | None = None
    ) -> int | None:
        """``choose`` for a deeper level of a menu: "back" (Esc, 0 when numbered) returns None."""
        return self._choose(prompt, options, default, disabled, True)

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
        requires: dict[int, set[int]] | None = None,
    ) -> list[int]:
        """Tick options (as a checkbox menu, else by numbers) and return the sorted indexes.

        ``checked`` are ticked at first and ``locked`` cannot be unticked; an option ticked
        brings the options ``requires`` says it needs. The answer is asked again until
        ``validate`` (given the indexes, locked and required ones included) returns None.
        """
        if not options:
            raise ValueError("options must not be empty")
        key = (prompt, tuple(options))
        checked = set(self._answers.get(key, checked))
        back = {"back": True} if self._back else {}
        with self._question(prompt):
            if self._select_many is not None:
                try:
                    ticks = self._select_many(prompt, options, checked, locked, validate, requires, **back)
                except EOFError:
                    raise CnsrError(f"缺少参数：{prompt}") from None
            else:
                ticks = self._tick(prompt, options, checked | locked, locked, validate, requires or {}, **back)
            if ticks is None:
                self._answered(BACK_STEP, False)
                raise GoBack
            self._answers[key] = list(ticks)
            self._answered("、".join(options[index].split("\t")[0] for index in ticks), False)  # rows: their first column
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

    def _pick(
        self, prompt: str, options: list[str], default: int | None, disabled: dict[int, str], back: bool
    ) -> int | None:
        """The index picked; None for "back" (Esc in a menu, 0 when numbered), offered when ``back``."""
        if self._select is not None:
            extra = {"default": default, "disabled": disabled} if default is not None or disabled else {}
            try:
                return self._select(prompt, options, **extra, **({"back": True} if back else {}))
            except EOFError:
                raise CnsrError(f"缺少参数：{prompt}") from None
        self._show(prompt)
        for number, cells in enumerate(aligned(options), 1):
            mark = f"（{disabled[number - 1]}）" if number - 1 in disabled else ""
            self._show(f"{number}. {COLUMN_GAP.join(cells)}{mark}")
        last = len(options)
        hint = f"请选择 [1-{last}]（直接回车：{default + 1}）" if default is not None else f"请选择 [1-{last}]"
        hint = f"{hint}（0 返回）：" if back else f"{hint}："
        while True:
            raw = self._read(self._input, hint, prompt).strip()
            if back and raw == "0":
                return None
            if not raw and default is not None:
                return default
            if raw.isdecimal() and int(raw) - 1 in disabled:
                self._show(f"第 {raw} 项{disabled[int(raw) - 1]}，不能选，请选别的。")
            elif raw.isdecimal() and 1 <= int(raw) <= last:
                return int(raw) - 1
            else:
                self._show(f"请输入 1 到 {last} 之间的数字。")

    def _tick(
        self,
        prompt: str,
        options: list[str],
        default: set[int],
        locked: set[int],
        validate: Callable[[list[int]], str | None],
        requires: dict[int, set[int]],
        back: bool = False,
    ) -> list[int] | None:
        """The numbered form of ``choose_many``: the numbers typed, or the default on Enter;
        None for 0 ("back"), offered when ``back``."""
        self._show(prompt)
        for number, cells in enumerate(aligned(options), 1):
            mark = "（已部署，不能取消）" if number - 1 in locked else ""
            self._show(f"{number}. {COLUMN_GAP.join(cells)}{mark}")
        last, initial = len(options), ",".join(str(index + 1) for index in sorted(default))
        extra = "，0 返回" if back else ""
        while True:
            raw = self._read(self._input, f"输入要选的编号，用逗号隔开（直接回车：{initial}{extra}）：", prompt).strip()
            if back and raw == "0":
                return None
            parts = [part.strip() for part in raw.replace("，", ",").split(",")] if raw else []
            if not all(part.isdecimal() and 1 <= int(part) <= last for part in parts):
                self._show(f"请输入 1 到 {last} 之间的编号，用逗号隔开。")
                continue
            typed = {int(part) - 1 for part in parts} | locked if raw else default
            ticks = sorted(with_required(typed, requires))
            message = validate(ticks)
            if message is None:
                return ticks
            self._show(message)

    @contextmanager
    def _question(self, prompt: str) -> Iterator[None]:
        """Log the question and keep the display paused until it is answered or interrupted."""
        self._require_interactive(prompt)
        self._asked += 1
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


def with_required(ticks: set[int], requires: dict[int, set[int]]) -> set[int]:
    """``ticks`` with every option they require, directly or through another."""
    found, todo = set(ticks), list(ticks)
    while todo:
        for needed in requires.get(todo.pop(), set()) - found:
            found.add(needed)
            todo.append(needed)
    return found


def width(text: str) -> int:
    """How many terminal cells ``text`` takes: wide (CJK) characters take two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def aligned(options: list[str]) -> list[list[str]]:
    """Each option's tab-separated columns, padded so that they line up across the options.

    A column whose cells are all a number and its unit ("0.5 GB 内存") is aligned right, so
    that the numbers line up; the others left. An option without a tab is one cell, left as it is; the last column is
    padded only when it is aligned right.
    """
    rows = [option.split("\t") for option in options]
    split = [cells for cells in rows if len(cells) > 1]
    widths: dict[int, int] = {}
    right: set[int] = set()
    for column in range(max((len(cells) for cells in split), default=0)):
        cells = [row[column] for row in split if column < len(row)]
        widths[column] = max(width(cell) for cell in cells)
        if all(_QUANTITY.match(cell) for cell in cells):
            right.add(column)

    def pad(cell: str, column: int, last: bool) -> str:
        gap = " " * (widths[column] - width(cell))
        return gap + cell if column in right else cell if last else cell + gap

    return [cells if len(cells) == 1 else [pad(cell, i, i == len(cells) - 1) for i, cell in enumerate(cells)] for cells in rows]


def plain(option: str) -> str:
    """``option`` with its columns joined as a line of text."""
    return COLUMN_GAP.join(option.split("\t"))
