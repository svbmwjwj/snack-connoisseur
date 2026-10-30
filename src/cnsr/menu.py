"""The arrow-key menus ``Ui.choose`` and ``Ui.choose_many`` show when cnsr runs in a terminal."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TextIO

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style

from cnsr.ui import BACK_STEP, COLUMN_GAP, aligned, plain, with_required

Select = Callable[..., int]  # (prompt, options, *, default=None, disabled=None) -> index
# (prompt, options, checked, locked, validate, requires) -> indexes
SelectMany = Callable[..., list[int]]

INSTRUCTION = "（上下方向键选择，回车确认）"
MANY_INSTRUCTION = "（上下方向键移动，空格勾选或取消，回车确认）"
BACK_HINT = "，Esc 返回）"
# How long a lone Esc waits for the rest of a key sequence: short, so that "back" feels immediate.
ESC_WAIT_SECONDS = 0.05
# Both menus: the option under the pointer in bold, ticks in green, what cannot be picked dim.
# An option split into columns (the size menus) colours them: the size, the type, the
# configuration, the traffic and the monthly price.
STYLE = Style.from_dict({
    "qmark": "fg:ansigreen bold", "question": "bold", "instruction": "fg:ansibrightblack", "answer": "fg:ansicyan",
    "pointer": "fg:ansicyan bold", "ticked": "fg:ansigreen bold", "unticked": "fg:ansibrightblack",
    "current": "bold", "locked": "fg:ansibrightblack", "disabled": "fg:ansibrightblack", "error": "fg:ansired",
    "column-0": "fg:ansicyan", "column-1": "fg:ansibrightblack", "column-5": "fg:ansiyellow", "column-6": "fg:ansigreen",
})  # fmt: skip
# Number keys jump to an option; past 9 options only the arrow keys (and j/k) move the pick.
SHORTCUT_LIMIT = 9


class Pick:
    """The options of a menu, numbered as the numbered prompts number them.

    The options in ``disabled`` keep their number, show their reason, and the pointer never
    stops on them.
    """

    def __init__(self, options: list[str], disabled: dict[int, str]):
        self.options, self.disabled = options, disabled

    def first(self, start: int) -> int:
        """``start``, or the next option after it that can be picked."""
        return self.move(start - 1, 1)

    def move(self, pointer: int, step: int) -> int:
        """The next option from ``pointer`` in direction ``step`` that can be picked."""
        for _ in self.options:
            pointer = (pointer + step) % len(self.options)
            if pointer not in self.disabled:
                return pointer
        raise ValueError("every option is disabled")

    def rows(self, pointer: int) -> list[tuple[str, str]]:
        """The menu's lines as prompt_toolkit (style, text) pairs: pointer, number and title."""
        tokens = []
        for index, cells in enumerate(aligned(self.options)):
            tokens.append(("class:pointer", " » " if index == pointer else "   "))
            if index in self.disabled:
                tokens.append(("class:disabled", f"{index + 1}) {COLUMN_GAP.join(cells)}（{self.disabled[index]}）"))
            else:
                current = "class:current" if index == pointer else ""
                tokens.append((current, f"{index + 1}) "))
                tokens.extend(_columns(cells, current))
            tokens.append(("", "\n"))
        return tokens


def _columns(cells: list[str], current: str) -> list[tuple[str, str]]:
    """The cells of one option, each in the style of its column when there are several."""
    if len(cells) == 1:
        return [(current, cells[0])]
    tokens = []
    for column, cell in enumerate(cells):
        style = " ".join(part for part in (current, f"class:column-{column}") if part)
        tokens.append((style, cell.rstrip()))
        tokens.append((current, cell[len(cell.rstrip()):] + (COLUMN_GAP if column < len(cells) - 1 else "")))
    return [token for token in tokens if token[1]]


def select(
    prompt: str,
    options: list[str],
    *,
    default: int | None = None,
    disabled: dict[int, str] | None = None,
    back: bool = False,
    **app: Any,
) -> int | None:
    """Show ``options`` as a menu and return the 0-based index of the pick; with ``back``, Esc
    returns None.

    The pointer starts on ``default`` (the next option that can be picked when it cannot be);
    only the option under the pointer is highlighted. The options in ``disabled`` show their
    reason and cannot be picked. Number keys move the pointer to that option, up to
    ``SHORTCUT_LIMIT``. Ctrl-C raises ``KeyboardInterrupt``, as everywhere else in cnsr.
    ``app`` goes to prompt_toolkit's ``Application`` (the tests hand it a pipe for input).
    """
    menu = Pick(options, disabled or {})
    state = {"pointer": menu.first(default or 0), "done": False}
    keys = KeyBindings()

    @keys.add("up")
    @keys.add("k")
    def _up(event: Any) -> None:
        state["pointer"] = menu.move(state["pointer"], -1)

    @keys.add("down")
    @keys.add("j")
    def _down(event: Any) -> None:
        state["pointer"] = menu.move(state["pointer"], 1)

    for number in range(1, min(len(options), SHORTCUT_LIMIT) + 1):

        @keys.add(str(number))
        def _jump(event: Any, index: int = number - 1) -> None:
            if index not in menu.disabled:
                state["pointer"] = index

    @keys.add("enter")
    def _enter(event: Any) -> None:
        state["done"] = True
        event.app.exit(result=state["pointer"])

    if back:
        _bind_back(keys, state)

    def lines() -> list[tuple[str, str]]:
        if state.get("back"):
            return _answer(prompt, BACK_STEP)
        if state["done"]:
            return _answer(prompt, plain(options[state["pointer"]]))
        return [*_question(prompt, _instruction(INSTRUCTION, back)), *menu.rows(state["pointer"])]

    return _run(lines, keys, app)


def _bind_back(keys: KeyBindings, state: dict[str, Any]) -> None:
    """Esc ends the menu with None: "back"."""

    @keys.add("escape")
    def _back(event: Any) -> None:
        state["back"] = True
        event.app.exit(result=None)


def _instruction(instruction: str, back: bool) -> str:
    return instruction[:-1] + BACK_HINT if back else instruction


def _question(prompt: str, instruction: str) -> list[tuple[str, str]]:
    return [("class:qmark", "? "), ("class:question", f"{prompt} "), ("class:instruction", f"{instruction}\n")]


def _answer(prompt: str, answer: str) -> list[tuple[str, str]]:
    return [("class:qmark", "? "), ("class:question", f"{prompt} "), ("class:answer", f"{answer}\n")]


def _run(lines: Callable[[], list[tuple[str, str]]], keys: KeyBindings, app: dict[str, Any]) -> Any:
    """Run a menu drawn by ``lines`` until a key binding exits it; Ctrl-C raises ``KeyboardInterrupt``."""

    @keys.add("c-c")
    def _interrupt(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    # Lines wider than the terminal wrap: cut, they would hide the end of an option (a price).
    window = Window(FormattedTextControl(lines, show_cursor=False), always_hide_cursor=True, wrap_lines=True)
    application = Application(Layout(HSplit([window])), key_bindings=keys, style=STYLE, **app)
    application.ttimeoutlen = ESC_WAIT_SECONDS
    return application.run()


class Ticks:
    """The ticks of a checkbox menu, which keep every ticked option's requirements ticked.

    ``requires`` maps an option to the options it needs: ticking it ticks them, and unticking
    one of them unticks it. ``locked`` options stay ticked.
    """

    def __init__(self, options: list[str], checked: set[int], locked: set[int], requires: dict[int, set[int]]):
        self.options, self.locked, self.requires = options, locked, requires
        self.ticked = with_required(checked | locked, requires)

    def toggle(self, index: int) -> None:
        if index in self.locked:
            return
        if index in self.ticked:
            self.ticked -= {other for other in self.ticked if index in with_required({other}, self.requires)}
        else:
            self.ticked |= with_required({index}, self.requires)

    def answer(self) -> list[int]:
        return sorted(self.ticked)

    def rows(self, pointer: int) -> list[tuple[str, str]]:
        """The menu's lines as prompt_toolkit (style, text) pairs: pointer, checkbox, title."""
        tokens = []
        for index, cells in enumerate(aligned(self.options)):
            ticked = index in self.ticked
            tokens.append(("class:pointer", " » " if index == pointer else "   "))
            tokens.append(("class:ticked" if ticked else "class:unticked", "[✓] " if ticked else "[ ] "))
            if index in self.locked:
                tokens.append(("class:locked", f"{COLUMN_GAP.join(cells)}（已部署）"))
            else:
                tokens.extend(_columns(cells, "class:current" if index == pointer else ""))
            tokens.append(("", "\n"))
        return tokens


def select_many(
    prompt: str,
    options: list[str],
    checked: set[int],
    locked: set[int],
    validate: Callable[[list[int]], str | None],
    requires: dict[int, set[int]] | None = None,
    back: bool = False,
    **app: Any,
) -> list[int] | None:
    """Show ``options`` as a checkbox menu and return the sorted indexes of the ticked ones.

    ``checked`` are ticked at first; ``locked`` stay ticked and cannot be changed; ticking an
    option ticks what ``requires`` says it needs (see ``Ticks``). Enter is refused, with
    ``validate``'s message, while the ticks do not pass it. With ``back``, Esc returns None.
    Ctrl-C raises ``KeyboardInterrupt``.
    ``app`` goes to prompt_toolkit's ``Application`` (the tests hand it a pipe for input).
    """
    ticks = Ticks(options, checked, locked, requires or {})
    state = {"pointer": 0, "error": "", "done": False}
    rows = len(options)
    keys = KeyBindings()

    @keys.add("up")
    @keys.add("k")
    def _up(event: Any) -> None:
        state["pointer"] = (state["pointer"] - 1) % rows

    @keys.add("down")
    @keys.add("j")
    def _down(event: Any) -> None:
        state["pointer"] = (state["pointer"] + 1) % rows

    @keys.add(" ")
    def _toggle(event: Any) -> None:
        ticks.toggle(state["pointer"])
        state["error"] = ""

    if back:
        _bind_back(keys, state)

    @keys.add("enter")
    def _enter(event: Any) -> None:
        state["error"] = validate(ticks.answer()) or ""
        if not state["error"]:
            state["done"] = True
            event.app.exit(result=ticks.answer())

    def lines() -> list[tuple[str, str]]:
        if state.get("back"):
            return _answer(prompt, BACK_STEP)
        if state["done"]:
            return _answer(prompt, "、".join(options[index].split("\t")[0] for index in ticks.answer()))
        tail = [("class:error", f"{state['error']}\n")] if state["error"] else []
        return [*_question(prompt, _instruction(MANY_INSTRUCTION, back)), *ticks.rows(state["pointer"]), *tail]

    return _run(lines, keys, app)


def terminal_select(stdin: TextIO | None = None, stdout: TextIO | None = None) -> Select | None:
    """``select`` when both stdin and stdout are a terminal; None means numbered prompts."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if stdin is None or stdout is None:  # pythonw has no console streams
        return None
    return select if stdin.isatty() and stdout.isatty() else None


def terminal_select_many(stdin: TextIO | None = None, stdout: TextIO | None = None) -> SelectMany | None:
    """``select_many`` when ``terminal_select`` would show a menu; None means numbered prompts."""
    return select_many if terminal_select(stdin, stdout) is not None else None
