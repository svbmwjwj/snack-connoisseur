"""Tests for the arrow-key menu behind ``Ui.choose``, driven by real key presses."""

from __future__ import annotations

import io

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cnsr.menu import select, terminal_select

DOWN, UP, ENTER = "\x1b[B", "\x1b[A", "\r"
KEYS = ["sg-node", "us-node", "jp-node", "github"]


def pick(keys: str, options: list[str] = KEYS) -> int:
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return select("选择这台节点使用的钥匙", options, input=pipe, output=DummyOutput())


def test_enter_takes_the_first_option():
    assert pick(ENTER) == 0


def test_the_arrow_keys_move_the_pick():
    assert pick(DOWN + DOWN + ENTER) == 2
    assert pick(DOWN + DOWN + UP + ENTER) == 1


def test_a_number_key_jumps_to_its_option():
    assert pick("4" + ENTER) == 3


def test_more_options_than_number_keys_still_work_with_the_arrows():
    many = [f"key-{n}" for n in range(12)]
    assert pick(DOWN * 10 + ENTER, many) == 10


def test_ctrl_c_interrupts_like_everywhere_else():
    with pytest.raises(KeyboardInterrupt):
        pick("\x03")


class Stream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize("stdin_tty, stdout_tty", [(False, True), (True, False), (False, False)])
def test_no_menu_unless_both_streams_are_a_terminal(stdin_tty, stdout_tty):
    assert terminal_select(Stream(stdin_tty), Stream(stdout_tty)) is None


def test_the_menu_in_a_terminal():
    assert terminal_select(Stream(True), Stream(True)) is select
