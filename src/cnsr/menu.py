"""The arrow-key menu ``Ui.choose`` shows when cnsr runs in a terminal."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TextIO

import questionary

Select = Callable[[str, list[str]], int]
SelectMany = Callable[[str, list[str], set[int], set[int], Callable[[list[int]], str | None]], list[int]]

INSTRUCTION = "（上下方向键选择，回车确认）"
MANY_INSTRUCTION = "（上下方向键移动，空格勾选或取消，回车确认）"
# Number keys jump to an option; past 9 options only the arrow keys (and j/k) move the pick.
SHORTCUT_LIMIT = 9


def select(prompt: str, options: list[str], **app: Any) -> int:
    """Show ``options`` as a menu and return the 0-based index of the pick.

    Ctrl-C raises ``KeyboardInterrupt``, as everywhere else in cnsr. ``app`` goes to
    prompt_toolkit's ``Application`` (the tests hand it a pipe for input).
    """
    choices = [questionary.Choice(option, value=index) for index, option in enumerate(options)]
    question = questionary.select(
        prompt, choices, instruction=INSTRUCTION, use_shortcuts=len(options) <= SHORTCUT_LIMIT, **app
    )
    return question.unsafe_ask()


def select_many(
    prompt: str,
    options: list[str],
    checked: set[int],
    locked: set[int],
    validate: Callable[[list[int]], str | None],
    **app: Any,
) -> list[int]:
    """Show ``options`` as a checkbox menu and return the sorted indexes of the ticked ones.

    ``checked`` are ticked at first; ``locked`` stay ticked and cannot be changed. Enter is
    refused, with ``validate``'s message, while the ticks (locked ones included) do not pass it.
    """
    choices = [
        questionary.Choice(option, value=index, checked=index in checked or index in locked,
                           disabled="已部署，不能取消" if index in locked else None)
        for index, option in enumerate(options)
    ]  # fmt: skip

    def ticks(values: list[int]) -> list[int]:
        return sorted(set(values) | locked)

    def check(values: list[int]) -> bool | str:
        return validate(ticks(values)) or True

    question = questionary.checkbox(prompt, choices, instruction=MANY_INSTRUCTION, validate=check, **app)
    return ticks(question.unsafe_ask())


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
