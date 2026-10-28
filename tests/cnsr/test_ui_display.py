import io
import logging

import pytest
from display_fakes import RecordingDisplay

from cnsr import runlog
from cnsr.errors import CnsrError
from cnsr.ui import Ui


def make(inputs):
    it = iter(inputs)
    out = io.StringIO()
    return Ui(input_fn=lambda p: next(it), secret_fn=lambda p: next(it), out=out, interactive=True), out


def recorded(*, inputs=(), secrets=(), select=None):
    typed, hidden = iter(inputs), iter(secrets)
    display = RecordingDisplay()
    seen = []

    def read(prompt):
        seen.append(display.paused_now)
        return next(typed)

    def read_secret(prompt):
        seen.append(display.paused_now)
        return next(hidden)

    def pick(prompt, options):
        seen.append(display.paused_now)
        return select(prompt, options) if select else 0

    ui = Ui(input_fn=read, secret_fn=read_secret, out=io.StringIO(), interactive=True,
            select_fn=pick if select else None, display=display)
    return ui, display, seen


def ui_records(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "cnsr.ui"]


def test_detail_goes_only_to_the_log(caplog):
    caplog.set_level(logging.INFO, logger="cnsr")
    ui, out = make([])
    ui.detail("安装 Docker")
    assert out.getvalue() == ""
    assert (logging.INFO, "安装 Docker") in ui_records(caplog)


def test_info_and_warn_go_to_screen_and_log(caplog):
    caplog.set_level(logging.INFO, logger="cnsr")
    ui, out = make([])
    ui.info("已完成")
    ui.warn("端口已被占用")
    assert out.getvalue() == "已完成\n警告：端口已被占用\n"
    assert ui_records(caplog) == [(logging.INFO, "已完成"), (logging.WARNING, "端口已被占用")]


def test_error_is_logged_at_error_level(caplog):
    caplog.set_level(logging.INFO, logger="cnsr")
    Ui(out=io.StringIO(), err=io.StringIO(), interactive=False).error("出错了")
    assert ui_records(caplog) == [(logging.ERROR, "出错了")]


def test_questions_pause_the_display():
    ui, display, seen = recorded(inputs=["jp-1", "已保存"], secrets=["hunter22"], select=lambda p, o: 1)
    assert ui.ask("别名") == "jp-1"
    assert ui.secret("令牌") == "hunter22"
    assert ui.choose("区域", ["a", "b"]) == 1
    ui.confirm_text("确认", "已保存")
    assert seen == [True] * 4
    assert display.paused_now is False


def test_numbered_choose_pauses_too():
    ui, display, seen = recorded(inputs=["2"])
    assert ui.choose("区域", ["a", "b"]) == 1
    assert seen == [True]
    assert display.paused_now is False


def test_ctrl_c_in_a_question_resumes_the_display():
    def interrupt(prompt, options):
        raise KeyboardInterrupt

    ui, display, seen = recorded(select=interrupt)
    with pytest.raises(KeyboardInterrupt):
        ui.choose("区域", ["a", "b"])
    assert seen == [True]
    assert display.paused_now is False
    assert display.calls[-1] == ("resumed",)


def test_eof_in_a_question_resumes_the_display():
    def eof(prompt):
        raise EOFError

    display = RecordingDisplay()
    ui = Ui(input_fn=eof, out=io.StringIO(), interactive=True, display=display)
    with pytest.raises(CnsrError):
        ui.ask("别名")
    assert display.paused_now is False


def test_error_prints_inside_a_pause():
    display = RecordingDisplay()
    Ui(out=io.StringIO(), err=io.StringIO(), interactive=False, display=display).error("出错了")
    assert display.calls == [("paused",), ("resumed",)]


def test_answers_are_logged_but_secrets_are_not(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="cnsr")
    registered = []
    monkeypatch.setattr(runlog, "secret", registered.append)
    ui, _, _ = recorded(inputs=["jp-1", "AKIAEXAMPLE0001", "2"], secrets=["hunter22"], select=None)
    ui.ask("别名")
    ui.secret("控制台密码")
    ui.ask("Access key ID", sensitive=True)
    ui.choose("区域", ["东京", "新加坡"])
    text = "\n".join(m for _, m in ui_records(caplog))
    assert "问：别名" in text and "答：jp-1" in text
    assert "hunter22" not in text and "AKIAEXAMPLE0001" not in text
    assert text.count("答：***") == 2
    assert "答：新加坡" in text
    assert registered == ["hunter22", "AKIAEXAMPLE0001"]


def test_confirm_text_logs_the_typed_text(caplog):
    caplog.set_level(logging.INFO, logger="cnsr")
    ui, _, _ = recorded(inputs=["已保存"])
    ui.confirm_text("存好后输入“已保存”", "已保存")
    assert ui_records(caplog) == [(logging.INFO, "问：存好后输入“已保存”"), (logging.INFO, "答：已保存")]


def test_stages_without_a_display_print_plain_lines():
    ui, out = make([])
    ui.begin("h", ["甲", "乙"])
    ui.stage(1)
    assert "[1/2] 甲" in out.getvalue()


def test_a_stage_can_close_the_one_before_as_failed():
    display = RecordingDisplay()
    ui = Ui(out=io.StringIO(), interactive=False, display=display)
    ui.begin("cnsr rm jp-1", ["甲", "乙"])
    ui.stage(1)
    ui.stage(2, previous="failed")
    assert display.calls[1:] == [("start", 1), ("start", 2, "failed")]


def test_stage_calls_reach_the_display_and_the_log(caplog):
    caplog.set_level(logging.INFO, logger="cnsr")
    display = RecordingDisplay()
    ui = Ui(out=io.StringIO(), interactive=False, display=display)
    ui.begin("cnsr up jp-1", ["甲", "乙"])
    ui.stage(2)
    ui.stage_count(1, 3)
    ui.busy("检查")
    ui.idle()
    with ui.transient("连接中"), ui.handover():
        pass
    ui.end("failed")
    assert display.calls == [
        ("begin", "cnsr up jp-1", ["甲", "乙"]), ("start", 2), ("count", 1, 3), ("busy", "检查"),
        ("idle",), ("transient", "连接中"), ("paused",), ("resumed",), ("end", "failed"),
    ]
    messages = [m for _, m in ui_records(caplog)]
    assert messages == ["cnsr up jp-1", "[2/2] 乙", "结束：失败"]
