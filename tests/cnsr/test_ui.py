import io
import sys

import pytest

from cnsr.errors import CnsrError
from cnsr.ui import Ui


def make(inputs, interactive=True):
    it = iter(inputs)
    out = io.StringIO()
    return Ui(input_fn=lambda p: next(it), secret_fn=lambda p: next(it), out=out, interactive=interactive), out


def make_recording(inputs, secrets=()):
    """Like make, but records the prompt text handed to each reader."""
    typed, hidden = iter(inputs), iter(secrets)
    asked, asked_secret = [], []

    def read(prompt):
        asked.append(prompt)
        return next(typed)

    def read_secret(prompt):
        asked_secret.append(prompt)
        return next(hidden)

    out = io.StringIO()
    return Ui(input_fn=read, secret_fn=read_secret, out=out, interactive=True), out, asked, asked_secret


def test_ask_uses_default_on_empty():
    ui, _ = make([""])
    assert ui.ask("登录用户", default="root") == "root"


def test_ask_repeats_until_valid():
    ui, out = make(["bad", "ok"])
    assert ui.ask("别名", validate=lambda s: None if s == "ok" else "格式不对") == "ok"
    assert "格式不对" in out.getvalue()


def test_choose_returns_index():
    ui, _ = make(["3", "2"])
    assert ui.choose("区域", ["jp", "sg"]) == 1


def test_confirm_text_loops_until_exact():
    ui, _ = make(["保存", "已保存"])
    ui.confirm_text("存好后输入“已保存”", "已保存")


def test_noninteractive_raises():
    ui, _ = make([], interactive=False)
    with pytest.raises(CnsrError, match="缺少参数：别名"):
        ui.ask("别名")


@pytest.mark.parametrize(
    "call",
    [
        lambda ui: ui.ask("别名"),
        lambda ui: ui.ask("别名", default="a"),
        lambda ui: ui.secret("令牌"),
        lambda ui: ui.choose("区域", ["jp", "sg"]),
        lambda ui: ui.confirm_text("输入“已保存”", "已保存"),
    ],
    ids=["ask", "ask-with-default", "secret", "choose", "confirm-text"],
)
def test_noninteractive_never_reads_input(call):
    ui, _ = make([], interactive=False)
    with pytest.raises(CnsrError, match="缺少参数："):
        call(ui)


def test_noninteractive_message_names_the_prompt():
    ui, _ = make([], interactive=False)
    with pytest.raises(CnsrError) as exc:
        ui.secret("Cloudflare 令牌")
    assert str(exc.value) == "缺少参数：Cloudflare 令牌"


def test_noninteractive_still_prints_progress():
    ui, out = make([], interactive=False)
    ui.begin("cnsr up jp-1", ["准备", "收尾"])
    ui.stage(1)
    ui.info("好了")
    assert out.getvalue() == "cnsr up jp-1\n[1/2] 准备\n好了\n"


def test_interactive_none_follows_stdin_tty(monkeypatch):
    class FakeStdin:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    monkeypatch.setattr(sys, "stdin", FakeStdin(False))
    with pytest.raises(CnsrError, match="缺少参数：别名"):
        Ui(out=io.StringIO()).ask("别名")

    monkeypatch.setattr(sys, "stdin", FakeStdin(True))
    ui = Ui(input_fn=lambda p: "x", out=io.StringIO())
    assert ui.ask("别名") == "x"


def test_explicit_interactive_overrides_tty(monkeypatch):
    class NoTty:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", NoTty())
    ui = Ui(input_fn=lambda p: "x", out=io.StringIO(), interactive=True)
    assert ui.ask("别名") == "x"


def test_ask_returns_typed_value_over_default():
    ui, _ = make(["admin"])
    assert ui.ask("登录用户", default="root") == "admin"


def test_ask_strips_surrounding_whitespace():
    ui, _ = make(["  jp-1  "])
    assert ui.ask("别名") == "jp-1"


def test_ask_whitespace_only_counts_as_empty():
    ui, _ = make(["   "])
    assert ui.ask("登录用户", default="root") == "root"


def test_ask_without_default_reasks_on_empty():
    ui, out = make(["", "  ", "a"])
    assert ui.ask("别名") == "a"
    assert out.getvalue().count("不能为空") == 2


def test_ask_empty_default_allows_empty_answer():
    ui, out = make([""])
    assert ui.ask("备注", default="") == ""
    assert out.getvalue() == ""


def test_ask_validates_the_default_too():
    ui, out = make(["", "ok"])
    got = ui.ask("别名", default="bad", validate=lambda s: None if s == "ok" else "格式不对")
    assert got == "ok"
    assert "格式不对" in out.getvalue()


def test_ask_validate_sees_the_empty_default():
    ui, out = make(["", "a"])
    assert ui.ask("备注", default="", validate=lambda s: None if s else "必须填写") == "a"
    assert "必须填写" in out.getvalue()


def test_ask_does_not_call_validate_on_missing_answer():
    seen = []
    ui, _ = make(["", "a"])
    ui.ask("别名", validate=lambda s: seen.append(s))
    assert seen == ["a"]


def test_ask_prompt_shows_default():
    ui, _, asked, _ = make_recording([""])
    ui.ask("登录用户", default="root")
    assert "登录用户" in asked[0]
    assert "[root]" in asked[0]


def test_ask_prompt_without_default_has_no_brackets():
    ui, _, asked, _ = make_recording(["a"])
    ui.ask("别名")
    assert "[" not in asked[0]
    assert "别名" in asked[0]


def test_secret_returns_value_without_echo():
    ui, out = make(["tok-secret-123"])
    assert ui.secret("令牌") == "tok-secret-123"
    assert "tok-secret-123" not in out.getvalue()


def test_secret_uses_secret_fn_not_input_fn():
    ui, _, asked, asked_secret = make_recording(inputs=[], secrets=["tok"])
    assert ui.secret("令牌") == "tok"
    assert asked == []
    assert "令牌" in asked_secret[0]


def test_secret_strips_surrounding_whitespace():
    ui, _ = make(["  tok\n"])
    assert ui.secret("令牌") == "tok"


def test_secret_reasks_on_empty_without_echo():
    ui, out = make(["", "tok-secret-123"])
    assert ui.secret("令牌") == "tok-secret-123"
    assert "tok-secret-123" not in out.getvalue()
    assert "不能为空" in out.getvalue()


def test_choose_lists_numbered_options():
    ui, out = make(["1"])
    assert ui.choose("区域", ["jp", "sg"]) == 0
    text = out.getvalue()
    assert "区域" in text
    assert "1. jp" in text
    assert "2. sg" in text


def test_choose_reasks_on_out_of_range_and_non_numeric():
    ui, out, asked, _ = make_recording(["0", "3", "abc", "", "-1", "2"])
    assert ui.choose("区域", ["jp", "sg"]) == 1
    assert len(asked) == 6
    assert out.getvalue().count("请输入 1 到 2 之间的数字") == 5


def test_choose_accepts_whitespace_around_number():
    ui, _ = make([" 2 "])
    assert ui.choose("区域", ["jp", "sg"]) == 1


def test_choose_accepts_full_width_digits():
    ui, _ = make(["２"])
    assert ui.choose("区域", ["jp", "sg"]) == 1


def test_choose_prints_options_once():
    ui, out = make(["9", "9", "1"])
    ui.choose("区域", ["jp", "sg"])
    assert out.getvalue().count("1. jp") == 1


def test_choose_hands_the_options_to_the_menu_when_there_is_one():
    seen = []

    def menu(prompt, options):
        seen.append((prompt, options))
        return 1

    out = io.StringIO()
    ui = Ui(input_fn=lambda p: pytest.fail("no typed answer with a menu"), out=out, interactive=True, select_fn=menu)
    assert ui.choose("区域", ["jp", "sg"]) == 1
    assert seen == [("区域", ["jp", "sg"])]
    assert out.getvalue() == ""  # the menu shows the prompt and the options itself


def test_choose_without_a_terminal_never_opens_the_menu():
    ui = Ui(out=io.StringIO(), interactive=False, select_fn=lambda p, o: pytest.fail("menu opened"))
    with pytest.raises(CnsrError, match="缺少参数：区域"):
        ui.choose("区域", ["jp", "sg"])


def test_eof_in_the_menu_becomes_cnsr_error():
    def closed(prompt, options):
        raise EOFError

    ui = Ui(out=io.StringIO(), interactive=True, select_fn=closed)
    with pytest.raises(CnsrError, match="缺少参数：区域"):
        ui.choose("区域", ["jp", "sg"])


def test_choose_rejects_empty_options():
    ui, _ = make([])
    with pytest.raises(ValueError):
        ui.choose("区域", [])


def test_confirm_text_accepts_first_exact_try():
    ui, out = make(["已保存"])
    ui.confirm_text("存好后输入“已保存”", "已保存")
    assert "请原样输入" not in out.getvalue()


def test_confirm_text_hints_on_mismatch():
    ui, out = make(["保存", "已保存"])
    ui.confirm_text("存好后输入“已保存”", "已保存")
    assert "请原样输入“已保存”" in out.getvalue()


def test_confirm_text_ignores_surrounding_whitespace_only():
    ui, _ = make([" 已保存 "])
    ui.confirm_text("存好后输入“已保存”", "已保存")
    ui, _ = make(["已 保存", "已保存"])
    ui.confirm_text("存好后输入“已保存”", "已保存")


def test_confirm_text_shows_prompt():
    ui, _, asked, _ = make_recording(["已保存"])
    ui.confirm_text("存好后输入“已保存”", "已保存")
    assert "存好后输入“已保存”" in asked[0]


def test_info_prints_message():
    ui, out = make([])
    ui.info("已完成")
    assert out.getvalue() == "已完成\n"


def test_warn_is_marked():
    ui, out = make([])
    ui.warn("端口已被占用")
    assert out.getvalue() == "警告：端口已被占用\n"


def test_eof_while_asking_becomes_cnsr_error():
    def eof(prompt):
        raise EOFError

    ui = Ui(input_fn=eof, secret_fn=eof, out=io.StringIO(), interactive=True)
    with pytest.raises(CnsrError, match="缺少参数：别名"):
        ui.ask("别名")
    with pytest.raises(CnsrError, match="缺少参数：令牌"):
        ui.secret("令牌")
    with pytest.raises(CnsrError, match="缺少参数：区域"):
        ui.choose("区域", ["jp"])
    with pytest.raises(CnsrError, match="缺少参数：确认"):
        ui.confirm_text("确认", "是")


def test_default_out_is_stdout_at_construction(capsys):
    Ui(interactive=False).info("你好")
    assert capsys.readouterr().out == "你好\n"


def test_error_goes_to_its_own_stream_with_the_prefix():
    out, err = io.StringIO(), io.StringIO()
    Ui(out=out, err=err, interactive=False).error("出错了")
    assert (out.getvalue(), err.getvalue()) == ("", "错误：出错了\n")


def test_error_defaults_to_stderr(capsys):
    Ui(interactive=False).error("出错了")
    assert capsys.readouterr().err == "错误：出错了\n"


FEATURES = ["基础加固", "Xray 节点"]


def needs_first(ticks):
    return None if 0 in ticks else "要先选第一项"


def test_choose_many_takes_the_default_on_enter():
    ui, out = make([""])
    assert ui.choose_many("功能", FEATURES, {0, 1}, set(), needs_first) == [0, 1]
    assert "1. 基础加固" in out.getvalue() and "2. Xray 节点" in out.getvalue()


def test_choose_many_takes_the_numbers_typed_in_any_comma():
    ui, _ = make(["1"])
    assert ui.choose_many("功能", FEATURES, {0, 1}, set(), needs_first) == [0]
    ui, _ = make(["2，1"])
    assert ui.choose_many("功能", FEATURES, set(), set(), needs_first) == [0, 1]


def test_choose_many_asks_again_until_the_check_passes():
    ui, out = make(["2", "9", "1,2"])
    assert ui.choose_many("功能", FEATURES, {0, 1}, set(), needs_first) == [0, 1]
    assert "要先选第一项" in out.getvalue() and "1 到 2 之间的编号" in out.getvalue()


def test_choose_many_keeps_locked_options_and_marks_them():
    ui, out = make(["1"])
    assert ui.choose_many("功能", FEATURES, {0}, {1}, needs_first) == [0, 1]
    assert "2. Xray 节点（已部署，不能取消）" in out.getvalue()


def test_choose_many_hands_everything_to_the_menu_when_there_is_one():
    seen = []

    def menu(prompt, options, checked, locked, validate):
        seen.append((prompt, options, checked, locked, validate([1])))
        return [0, 1]

    ui = Ui(input_fn=lambda p: pytest.fail("no typed answer with a menu"), out=io.StringIO(), interactive=True,
            select_many_fn=menu)  # fmt: skip
    assert ui.choose_many("功能", FEATURES, {0, 1}, {0}, needs_first) == [0, 1]
    assert seen == [("功能", FEATURES, {0, 1}, {0}, "要先选第一项")]


def test_choose_many_without_a_terminal_is_an_error():
    ui = Ui(out=io.StringIO(), interactive=False, select_many_fn=lambda *a: pytest.fail("menu opened"))
    with pytest.raises(CnsrError, match="缺少参数：功能"):
        ui.choose_many("功能", FEATURES, {0, 1}, set(), needs_first)
    assert ui.interactive is False


def test_eof_in_the_checkbox_menu_becomes_cnsr_error():
    def closed(*args):
        raise EOFError

    ui = Ui(out=io.StringIO(), interactive=True, select_many_fn=closed)
    with pytest.raises(CnsrError, match="缺少参数：功能"):
        ui.choose_many("功能", FEATURES, {0, 1}, set(), needs_first)
