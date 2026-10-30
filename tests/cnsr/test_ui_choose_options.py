"""``Ui.choose`` with a default pick and options that cannot be picked, numbered and as a menu."""

from __future__ import annotations

import io

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cnsr.menu import select
from cnsr.ui import Ui

DOWN, ENTER = "\x1b[B", "\r"
OPTIONS = ["1c1g", "2c4g", "4c8g"]


def numbered(answers: list[str]) -> tuple[Ui, io.StringIO, list[str]]:
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return answers.pop(0)

    out = io.StringIO()
    return Ui(input_fn=read, out=out, interactive=True), out, prompts


def test_enter_takes_the_default():
    ui, _, prompts = numbered([""])
    assert ui.choose("规格", OPTIONS, default=1) == 1
    assert prompts == ["请选择 [1-3]（直接回车：2）："]


def test_without_a_default_enter_asks_again():
    ui, out, _ = numbered(["", "3"])
    assert ui.choose("规格", OPTIONS) == 2
    assert "请输入 1 到 3 之间的数字。" in out.getvalue()


def test_a_disabled_option_shows_its_reason_and_is_refused():
    ui, out, _ = numbered(["3", "9", "2"])
    assert ui.choose("规格", OPTIONS, default=0, disabled={2: "该区域暂无货"}) == 1
    screen = out.getvalue()
    assert "3. 4c8g（该区域暂无货）" in screen and "1. 1c1g\n" in screen
    assert "第 3 项该区域暂无货，不能选，请选别的。" in screen
    assert "请输入 1 到 3 之间的数字。" in screen


def test_the_menu_gets_the_default_and_the_disabled_options():
    seen = []

    def menu(prompt, options, **extra):
        seen.append(extra)
        return 0

    ui = Ui(out=io.StringIO(), interactive=True, select_fn=menu)
    ui.choose("规格", OPTIONS, default=1, disabled={2: "该区域暂无货"})
    assert seen == [{"default": 1, "disabled": {2: "该区域暂无货"}}]


def test_a_plain_choice_hands_the_menu_nothing_more():
    ui = Ui(out=io.StringIO(), interactive=True, select_fn=lambda prompt, options: 1)
    assert ui.choose("区域", ["jp", "sg"]) == 1


def pick(keys: str, **options) -> int:
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return select("规格", OPTIONS, input=pipe, output=DummyOutput(), **options)


def test_the_menu_starts_on_the_default():
    assert pick(ENTER, default=1) == 1
    assert pick(DOWN + ENTER, default=1) == 2


def test_the_menu_skips_a_disabled_option():
    assert pick(DOWN + ENTER, disabled={1: "该区域暂无货"}) == 2
    assert pick(ENTER, default=2, disabled={0: "该区域暂无货"}) == 2
