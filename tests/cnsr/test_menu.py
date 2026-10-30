"""Tests for the arrow-key menu behind ``Ui.choose``, driven by real key presses."""

from __future__ import annotations

import io

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cnsr.menu import Pick, Ticks, select, select_many, terminal_select, terminal_select_many

DOWN, UP, ENTER = "\x1b[B", "\x1b[A", "\r"
KEYS = ["sg-node", "us-node", "jp-node", "github"]


def pick(keys: str, options: list[str] = KEYS, **extra) -> int:
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return select("选择这台节点使用的钥匙", options, input=pipe, output=DummyOutput(), **extra)


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


def test_the_pointer_starts_on_the_default():
    assert pick(ENTER, default=2) == 2
    assert pick(UP + ENTER, default=2) == 1


def test_the_arrows_and_number_keys_skip_a_disabled_option():
    assert pick(DOWN + ENTER, disabled={1: "不能选"}) == 2
    assert pick(UP + ENTER, default=2, disabled={1: "不能选"}) == 0
    assert pick("2" + ENTER, disabled={1: "不能选"}) == 0  # the number of a disabled option does nothing


def test_a_disabled_default_starts_on_the_next_option():
    assert pick(ENTER, default=0, disabled={0: "不能选"}) == 1


def test_a_number_key_counts_disabled_options_too():
    assert pick("4" + ENTER, disabled={2: "不能选"}) == 3


def rows(menu: Pick, pointer: int) -> str:
    return "".join(text for _, text in menu.rows(pointer))


def test_rows_number_every_option_and_show_why_one_cannot_be_picked():
    menu = Pick(["a", "b", "c", "d"], {2: "该区域暂无货"})
    assert rows(menu, 1) == "   1) a\n » 2) b\n   3) c（该区域暂无货）\n   4) d\n"


def test_only_the_option_under_the_pointer_is_highlighted():
    menu = Pick(["a", "b"], {})
    styles = [style for style, text in menu.rows(1) if text.strip() in ("1)", "a", "2)", "b")]
    assert styles == ["", "", "class:current", "class:current"]


def test_columns_line_up_and_each_has_its_colour_class():
    menu = Pick(["2c1g\tmicro", "16c64g\tx"], {})
    assert rows(menu, 0) == " » 1) 2c1g    micro\n   2) 16c64g  x\n"
    styles = [style for style, text in menu.rows(1) if text.strip() in ("2c1g", "16c64g", "micro", "x")]
    assert styles == ["class:column-0", "class:column-1", "class:current class:column-0", "class:current class:column-1"]


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


SPACE = " "
FEATURES = ["基础加固", "Xray 节点"]


def tick(keys: str, *, checked=frozenset({0, 1}), locked=frozenset(), validate=lambda ticks: None,
         requires=None) -> list[int]:
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return select_many("要部署哪些功能？", FEATURES, set(checked), set(locked), validate, requires,
                           input=pipe, output=DummyOutput())  # fmt: skip


def test_enter_keeps_what_is_ticked_at_first():
    assert tick(ENTER) == [0, 1]
    assert tick(ENTER, checked=frozenset({0})) == [0]


def test_space_toggles_the_option_under_the_pointer():
    assert tick(DOWN + SPACE + ENTER) == [0]
    assert tick(SPACE + ENTER) == [1]


def test_ticking_an_option_ticks_what_it_requires():
    assert tick(SPACE + DOWN + SPACE + ENTER, requires={1: {0}}) == [0, 1]  # untick both, then tick Xray
    assert tick(DOWN + SPACE + UP + SPACE + DOWN + SPACE + ENTER, checked=frozenset({0}), requires={1: {0}}) == [0, 1]


def test_unticking_an_option_unticks_what_requires_it():
    assert tick(SPACE + ENTER, requires={1: {0}}) == []


ESC = "\x1b"


def test_esc_in_the_checkbox_returns_none_when_back_is_offered():
    with create_pipe_input() as pipe:
        pipe.send_text(ESC)
        assert select_many("功能", FEATURES, {0, 1}, set(), lambda ticks: None, back=True,
                           input=pipe, output=DummyOutput()) is None  # fmt: skip


def test_esc_in_a_menu_returns_none_when_back_is_offered():
    assert pick(ESC, back=True) is None


def test_esc_does_nothing_without_back():
    assert pick(ESC + DOWN + ENTER) == 1


def plain(ticks: Ticks) -> str:
    return "".join(text for _, text in ticks.rows(pointer=1))


def test_rows_show_a_checkbox_per_option_and_the_pointer():
    ticks = Ticks(FEATURES, {0}, set(), {})
    assert plain(ticks) == "   [✓] 基础加固\n » [ ] Xray 节点\n"


def test_a_locked_option_shows_that_it_stays():
    assert "[✓] 基础加固（已部署）" in plain(Ticks(FEATURES, set(), {0}, {}))


def test_enter_is_refused_until_the_ticks_pass_the_check():
    def needs_first(ticks):
        return None if 0 in ticks else "要先勾第一项"

    # Unticking the first item is refused at Enter; ticking it again lets Enter through.
    assert tick(SPACE + ENTER + SPACE + ENTER, validate=needs_first) == [0, 1]


def test_a_locked_option_stays_ticked_and_is_part_of_the_answer():
    seen = []

    def record(ticks):
        seen.append(ticks)
        return None

    assert tick(ENTER, checked=frozenset({0}), locked=frozenset({1}), validate=record) == [0, 1]
    assert seen[-1] == [0, 1]


def test_ctrl_c_interrupts_the_checkbox_too():
    with pytest.raises(KeyboardInterrupt):
        tick("\x03")


def test_the_checkbox_menu_only_in_a_terminal():
    assert terminal_select_many(Stream(False), Stream(True)) is None
    assert terminal_select_many(Stream(True), Stream(True)) is select_many
