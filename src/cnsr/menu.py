"""The arrow-key menu ``Ui.choose`` shows when cnsr runs in a terminal."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TextIO

import questionary

Select = Callable[[str, list[str]], int]

INSTRUCTION = "（上下方向键选择，回车确认）"
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


def terminal_select(stdin: TextIO | None = None, stdout: TextIO | None = None) -> Select | None:
    """``select`` when both stdin and stdout are a terminal; None means numbered prompts."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if stdin is None or stdout is None:  # pythonw has no console streams
        return None
    return select if stdin.isatty() and stdout.isatty() else None
