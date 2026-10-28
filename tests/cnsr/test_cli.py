import os
import signal
import sys
import time
import tomllib
import types
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

import cnsr
from cnsr import __version__
from cnsr.cli import HELP_TEXT, main, parse_args
from cnsr.errors import CnsrError


def test_no_args_prints_help(capsys):
    assert main([]) == 0
    assert capsys.readouterr().out == HELP_TEXT


def test_help_flag_prints_help(capsys):
    assert main(["-h"]) == 0
    out = capsys.readouterr().out
    assert "up [别名]" in out and "--aws <区域>" in out and "--ip <IP>" in out and "rm [别名]" in out


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"cnsr {__version__}\n"


def test_commands_refuse_to_run_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    assert main(["list"]) == 1
    assert capsys.readouterr().err == "cnsr 暂不支持 Windows，请在 macOS 或 Linux 上运行。\n"
    assert main([]) == 0 and main(["--version"]) == 0


def test_the_version_comes_from_pyproject_alone():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]
    assert pyproject["project"]["version"] not in (Path(cnsr.__file__)).read_text(encoding="utf-8")


def test_the_version_without_installed_metadata():
    def missing(name):
        raise PackageNotFoundError(name)

    assert cnsr._installed_version(missing) == "0+unknown"


def test_parse_up_aws():
    ns = parse_args(["up", "jp-1", "--aws", "jp"])
    assert (ns.command, ns.alias, ns.aws, ns.ip, ns.static_ip) == ("up", "jp-1", "jp", None, None)


def test_parse_up_aws_with_a_static_ip():
    ns = parse_args(["up", "sg-1", "--aws", "sg", "--static-ip", "203.0.113.50"])
    assert (ns.aws, ns.static_ip) == ("sg", "203.0.113.50")


@pytest.mark.parametrize("argv", [
    ["up", "x", "--aws", "jp", "--ip", "192.0.2.1"],
    ["up", "x", "--aws", "eu"],
])
def test_parse_rejects(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


@pytest.fixture(autouse=True)
def config_home(monkeypatch, tmp_path):
    """Keep run logs out of the real configuration directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path / "config" / "cnsr"


@pytest.fixture
def commands(monkeypatch):
    """Install fake cnsr.commands.up / cnsr.commands.rm modules and record their calls."""
    calls = {}
    package = types.ModuleType("cnsr.commands")
    package.__path__ = []
    for name in ("up", "rm", "listing"):
        module = types.ModuleType(f"cnsr.commands.{name}")

        def run(ns, name=name):
            calls[name] = ns
            return 0

        module.run = run
        setattr(package, name, module)
        monkeypatch.setitem(sys.modules, f"cnsr.commands.{name}", module)
    monkeypatch.setitem(sys.modules, "cnsr.commands", package)
    return calls


@pytest.mark.parametrize("argv", [["rm", "-h"], ["up", "-h"], ["up", "x", "--help"]])
def test_help_flag_after_command(argv, capsys):
    assert main(argv) == 0
    assert capsys.readouterr().out == HELP_TEXT


def test_dispatch_up(commands):
    assert main(["up", "jp-1", "--aws", "jp"]) == 0
    assert list(commands) == ["up"]
    assert (commands["up"].alias, commands["up"].aws) == ("jp-1", "jp")


def test_dispatch_rm(commands):
    assert main(["rm", "jp-1"]) == 0
    assert list(commands) == ["rm"]
    assert commands["rm"].alias == "jp-1"


def test_dispatch_list(commands):
    assert main(["list"]) == 0
    assert list(commands) == ["listing"]


def test_dispatch_returns_command_exit_code(monkeypatch, commands):
    monkeypatch.setattr(sys.modules["cnsr.commands.up"], "run", lambda ns: 7)
    assert main(["up", "x", "--ip", "192.0.2.1"]) == 7


def test_cnsr_error_prints_message_and_returns_1(monkeypatch, commands, capsys):
    def boom(ns):
        raise CnsrError("服务器不是全新系统")

    monkeypatch.setattr(sys.modules["cnsr.commands.rm"], "run", boom)
    assert main(["rm", "jp-1"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "错误：服务器不是全新系统\n"
    assert captured.out.startswith("日志：") and captured.out.count("\n") == 1


def test_keyboard_interrupt_returns_130(monkeypatch, commands, capsys):
    def interrupted(ns):
        raise KeyboardInterrupt

    monkeypatch.setattr(sys.modules["cnsr.commands.up"], "run", interrupted)
    assert main(["up", "x", "--aws", "sg"]) == 130
    assert capsys.readouterr().err == "已中断。修好问题后重跑同一条命令即可继续。\n"


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGHUP on Windows")
# Signals by name: signal.SIGHUP does not exist on Windows, and parametrize is evaluated at import.
@pytest.mark.parametrize("name", ["SIGHUP", "SIGTERM"])
def test_a_closed_terminal_is_an_interrupt_that_cleans_up(monkeypatch, commands, capsys, name):
    signum = getattr(signal, name)
    cleaned = []

    def hung_up(ns):
        try:
            os.kill(os.getpid(), signum)
            time.sleep(5)  # the handler interrupts this
        except KeyboardInterrupt:
            cleaned.append(True)  # what the deploy's own Ctrl-C cleanup sees
            raise

    before = signal.getsignal(signum)
    monkeypatch.setattr(sys.modules["cnsr.commands.up"], "run", hung_up)
    assert main(["up", "x", "--aws", "sg"]) == 130
    assert cleaned == [True] and "已中断" in capsys.readouterr().err
    assert signal.getsignal(signum) == before


@pytest.mark.parametrize("argv,expected", [
    (["up", "x", "--aws", "eu"], "jp、sg、us"),
    (["up", "x", "--aws", "jp", "--ip", "192.0.2.1"], "不能同时使用"),
    (["up", "x", "--ip", "192.0.2.1", "--aws", "jp"], "不能同时使用"),
    (["nope"], "未知命令"),
    (["up", "x", "--aws"], "不正确"),
])
def test_usage_errors_are_reported_in_chinese(argv, expected, capsys):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("错误：") and expected in err
    assert "usage" not in err and "error" not in err and "argument" not in err


@pytest.mark.parametrize("argv, word", [(["jp-test", "--aws", "jp"], "jp-test"), (["--aws", "jp"], "jp")])
def test_a_forgotten_command_word_is_named_with_the_way_to_write_it(argv, word, capsys):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert f"未知命令“{word}”" in err and "cnsr up <别名>" in err and "cnsr rm <别名>" in err


def test_windows_forces_utf8_output(monkeypatch):
    class FakeStream:
        def __init__(self):
            self.encoding = None

        def reconfigure(self, *, encoding):
            self.encoding = encoding

        def write(self, text):
            return len(text)

    out, err = FakeStream(), FakeStream()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert main([]) == 0
    assert (out.encoding, err.encoding) == ("utf-8", "utf-8")


def _interrupt(ns):
    raise KeyboardInterrupt


def _fail(ns):
    raise CnsrError("服务器不是全新系统")


@pytest.mark.parametrize("command, outcome, code", [
    (lambda ns: 0, "ok", 0),
    (lambda ns: 4, "exit-4", 4),
    (_fail, "error", 1),
    (_interrupt, "interrupted", 130),
])
def test_the_log_path_is_the_last_line(command, outcome, code, monkeypatch, commands, capsys, config_home):
    monkeypatch.setattr(sys.modules["cnsr.commands.up"], "run", command)
    assert main(["up", "jp-1", "--aws", "jp"]) == code
    captured = capsys.readouterr()
    last = captured.out.splitlines()[-1]
    assert last.startswith("日志：")
    path = Path(last.removeprefix("日志："))
    assert path.is_absolute() and path.is_file() and path.parent == config_home / "logs"
    text = path.read_text(encoding="utf-8")
    assert "jp-1" in path.name and "['up', 'jp-1', '--aws', 'jp']" in text
    if outcome == "interrupted":
        assert "已中断" in text and captured.err == "已中断。修好问题后重跑同一条命令即可继续。\n"
    else:
        assert f"退出码 {code}" in text
    if outcome == "error":
        assert captured.err == "错误：服务器不是全新系统\n" and "服务器不是全新系统" in text


def test_the_log_belongs_to_the_command_that_ran(monkeypatch, commands, capsys, config_home):
    assert main(["rm", "jp-1"]) == 0
    (log,) = (config_home / "logs").iterdir()
    assert log.name.startswith("rm-jp-1_")
    assert "命令 rm" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", [["-h"], [], ["nope"], ["--version"], ["up", "-h"]])
def test_help_and_usage_errors_write_no_log(argv, commands, config_home):
    main(argv)
    assert not (config_home / "logs").exists()


def test_no_log_line_when_the_log_cannot_be_written(commands, capsys, config_home):
    config_home.mkdir(parents=True)
    (config_home / "logs").write_text("not a directory", encoding="utf-8")
    assert main(["up", "jp-1", "--aws", "jp"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("警告：无法写入运行日志（")
    assert captured.err.count("警告：") == 1 and "无法写日志" not in captured.err
    assert list(commands) == ["up"]


def test_an_unexpected_error_is_reported_and_still_ends_with_the_log_line(monkeypatch, commands, capsys, config_home):
    def bug(ns):
        raise RuntimeError("something odd")

    monkeypatch.setattr(sys.modules["cnsr.commands.up"], "run", bug)
    assert main(["up", "jp-1", "--aws", "jp"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "错误：意外错误（RuntimeError：something odd），详情见日志\n"
    last = captured.out.splitlines()[-1]
    assert last.startswith("日志：")
    text = Path(last.removeprefix("日志：")).read_text(encoding="utf-8")
    assert "Traceback" in text and "RuntimeError: something odd" in text
    assert "退出码 1" in text.splitlines()[-1]


def test_ctrl_c_while_the_log_is_being_set_up_returns_130(monkeypatch, commands, capsys):
    def interrupted_setup(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cnsr.runlog, "started", interrupted_setup)
    assert main(["up", "jp-1", "--aws", "jp"]) == 130
    captured = capsys.readouterr()
    assert captured.err == "已中断。修好问题后重跑同一条命令即可继续。\n"
    assert captured.out == "" and list(commands) == []


def test_rm_without_an_alias_leaves_it_to_the_command():
    assert parse_args(["rm"]).alias is None


@pytest.mark.parametrize("pick, command", [(0, "up"), (1, "rm"), (2, "listing")])
def test_no_command_in_a_terminal_opens_the_menu(monkeypatch, commands, pick, command):
    import cnsr.menu

    asked = []

    def menu(prompt, options):
        asked.append((prompt, options))
        return pick

    monkeypatch.setattr(cnsr.menu, "terminal_select", lambda: menu)
    assert main([]) == 0
    assert asked == [("要做什么？", ["部署服务器", "删除服务器", "服务器列表"])]
    assert list(commands) == [command] and commands[command].alias is None


def test_no_command_without_a_terminal_prints_help(monkeypatch, capsys):
    import cnsr.menu

    monkeypatch.setattr(cnsr.menu, "terminal_select", lambda: None)
    assert main([]) == 0
    assert capsys.readouterr().out == HELP_TEXT
