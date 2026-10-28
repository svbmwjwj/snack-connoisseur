import io
import re

import pytest
from rich.cells import cell_len
from rich.console import Console

from cnsr.progress import LiveStages, PlainStages, elapsed, terminal_display

HEADING = "cnsr up jp-1 · AWS 东京"
TITLES = ["甲", "乙", "丙"]
ALT_ON, ALT_OFF = "\x1b[?1049h", "\x1b[?1049l"
CONTROL = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def plain():
    out, clock = io.StringIO(), FakeClock()
    return PlainStages(out, clock=clock), out, clock


def live(width=80):
    file, clock = io.StringIO(), FakeClock()
    console = Console(file=file, force_terminal=True, width=width, color_system=None)
    return LiveStages(console, auto_refresh=False, clock=clock), file, clock


def test_elapsed():
    assert elapsed(7) == "0:07"
    assert elapsed(157) == "2:37"
    assert elapsed(3723) == "1:02:03"


@pytest.mark.parametrize(
    ("outcome", "word"),
    [("failed", "失败"), ("interrupted", "已中断"), ("done", "完成")],
)
def test_plain_stages_print_one_line_per_start_and_end(outcome, word):
    stages, out, clock = plain()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    clock.now += 5
    stages.start(2)
    clock.now += 1
    stages.end(outcome)
    assert out.getvalue() == (
        f"{HEADING}\n[1/3] 甲\n[1/3] 甲：完成（0:05）\n[2/3] 乙\n[2/3] 乙：{word}（0:01）\n"
    )


def test_plain_start_can_close_the_previous_stage_as_failed():
    stages, out, clock = plain()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    clock.now += 5
    stages.start(2, previous="failed")
    stages.end("done")
    assert out.getvalue() == f"{HEADING}\n[1/3] 甲\n[1/3] 甲：失败（0:05）\n[2/3] 乙\n[2/3] 乙：完成（0:00）\n"


def test_live_start_can_close_the_previous_stage_as_failed():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    stages.start(2, previous="failed")
    stages.start(3)
    stages.end("done")
    text = file.getvalue()
    assert "✗ 甲" in text and "✓ 乙" in text and "✓ 丙" in text and "2/3" in text


def test_plain_busy_line_count_and_transient():
    stages, out, _ = plain()
    stages.busy("检查控制端")
    assert out.getvalue() == "检查控制端……\n"
    stages.count(1, 2)
    with stages.paused():
        pass
    stages.idle()
    assert out.getvalue() == "检查控制端……\n"
    with stages.transient("密码 X"):
        pass
    assert out.getvalue() == "检查控制端……\n密码 X\n"


def test_live_end_leaves_the_final_block():
    stages, file, clock = live()
    stages.begin(HEADING, TITLES)
    for number in (1, 2, 3):
        stages.start(number)
        clock.now += 2
    stages.end("done")
    text = file.getvalue()
    for title in TITLES:
        assert f"✓ {title}" in text
    assert "3/3" in text
    assert HEADING in text


def test_live_marks_a_failed_stage():
    stages, file, clock = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    stages.start(2)
    clock.now += 1
    stages.end("failed")
    assert "✗ 乙" in file.getvalue()


def test_live_marks_an_interrupted_stage():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    stages.end("interrupted")
    assert "已中断" in file.getvalue()


def test_live_count_shows_the_numbers():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    stages.count(1534, 3071)
    stages.refresh()
    assert "1534 / 3071" in file.getvalue()


def test_live_pause_leaves_one_context_line_per_stage():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(2)
    with stages.paused():
        pass
    with stages.paused():
        pass
    assert file.getvalue().count("[2/3] 乙") == 1


def test_live_pause_resumes_after_an_interrupt():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    with pytest.raises(KeyboardInterrupt), stages.paused():
        raise KeyboardInterrupt
    before = len(file.getvalue())
    stages.refresh()
    assert len(file.getvalue()) > before


def test_live_transient_uses_the_alternate_screen():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    with stages.transient("密码 X"):
        pass
    text = file.getvalue()
    on, off = text.index(ALT_ON), text.index(ALT_OFF)
    assert text.count("密码 X") == 1
    assert on < text.index("密码 X") < off


def test_live_line_is_printed():
    stages, file, _ = live()
    stages.begin(HEADING, TITLES)
    stages.start(1)
    stages.line("警告：[x] 不是标记")
    assert "警告：[x] 不是标记" in file.getvalue()


def test_live_busy_shows_a_spinner_line_until_idle():
    stages, file, _ = live()
    stages.busy("检查控制端")
    stages.refresh()
    assert "检查控制端" in file.getvalue()
    stages.idle()
    before = len(file.getvalue())
    stages.refresh()
    assert len(file.getvalue()) == before


def test_live_fits_a_narrow_terminal():
    long_title = "很" * 40
    stages, file, clock = live(width=30)
    stages.begin(f"cnsr up {long_title}", [long_title, "乙"])
    stages.start(1)
    stages.count(1534, 3071)
    clock.now += 75
    stages.refresh()
    with stages.paused():
        pass
    stages.end("failed")
    text = CONTROL.sub("", file.getvalue())
    assert "✗ 很" in text and "…" in text
    for line in re.split(r"[\r\n]", text):
        assert cell_len(line) <= 30, line


class FakeStdout(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def test_terminal_display_picks_by_tty(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    assert isinstance(terminal_display(FakeStdout(True)), LiveStages)
    assert isinstance(terminal_display(FakeStdout(False)), PlainStages)
