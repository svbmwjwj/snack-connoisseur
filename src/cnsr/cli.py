"""Command-line entry point: argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

from cnsr import __version__, paths, runlog
from cnsr.errors import CnsrError

HELP_TEXT = """\
cnsr  部署并加固服务器，可选自建 REALITY 节点（SNI 失效时自动更换）

用法  cnsr <命令> [参数]；在终端里不带命令运行 cnsr 时显示菜单

命令
  up [别名]             部署服务器：勾选要部署的功能（基础加固、Xray 节点）；
                        不带参数时逐项询问
      --aws <区域>      在 AWS Lightsail 新开一台（区域如 jp-tokyo、sg、us-oregon、
                        de-frankfurt，共 19 个；不写时菜单里选“更多地区”可看全部）
      --static-ip <IP>  配合 --aws：用你在这个区域已有的静态 IP 作服务器地址；
                        它绑在别的实例上时先问你再改绑，cnsr rm 时解绑并保留；
                        不写时，在终端里运行会用菜单列出区域内已有的静态 IP
      --hetzner <区域>  在 Hetzner Cloud 新开一台（区域：sg、us-oregon、us-virginia、
                        de-falkenstein、de-nuremberg、fi-helsinki）；API token 先读
                        环境变量 HCLOUD_TOKEN，再读本机保存的，都没有时询问
      --size <规格>     配合 --aws：至少几核几 GB 内存，例如 2c0.5g、2c1g，自动选满足它、
                        最便宜的套餐；配合 --hetzner：机型型号，例如 cx23、cpx22、ccx13；
                        不写时用菜单选（Hetzner 先选方案再选机型）
      --ip <IP>         接管一台已有服务器
      --to <功能>       部署到哪一层：base 只加固，xray 加固并部署 Xray 节点；
                        不写时用菜单勾选
  rm [别名]             删除服务器；AWS 服务器同时删除实例、释放静态 IP；Hetzner 服务器
                        同时删除服务器、Primary IP 和防火墙（SSH key 保留）；不写别名时
                        列出所有服务器勾选，可多选；删除前确认一次（直接回车为取消）
  list                  服务器列表，云服务器带本月流量（AWS 为入站加出站）；有 Xray 节点时
                        同时列出圈X 和 v2rayN 订阅地址，以及加到圈X [task_local] 的检测任务

新开服务器时，开始部署前会打印等价命令（不含 --static-ip），换一个别名即可按同样的配置再开一台。
对已有别名再次运行 up：续跑没完成的步骤、可以补选功能、补充备选 SNI，并更新到当前版本。

选项
  -h, --help            显示本帮助
  --version             显示版本
"""

# ``--aws``: the codes of ``cnsr.aws.lightsail.REGIONS`` (not imported here: it loads boto3).
AWS_REGIONS = (
    "jp-tokyo", "sg", "us-oregon", "kr-seoul", "hk", "id-jakarta", "my", "in-mumbai", "au-sydney", "us-virginia",
    "us-ohio", "ca-montreal", "br-sao-paulo", "de-frankfurt", "ie-dublin", "uk-london", "fr-paris", "se-stockholm", "es",
)  # fmt: skip
HETZNER_REGIONS = ("sg", "us-oregon", "us-virginia", "de-falkenstein", "de-nuremberg", "fi-helsinki")
# ``--to``: the feature to deploy up to, with every feature it builds on.
TO_FEATURES = ("base", "xray")
# The top-level menu of ``cnsr`` run without a command, and the command behind each entry.
MENU_LABELS = ["部署服务器", "删除服务器", "服务器列表"]
MENU_COMMANDS = ["up", "rm", "list"]
# argparse: "argument command: invalid choice: 'jp-1' (choose from add, rm)"
_INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")
# The sources of ``cnsr up``, in the order a clash between two of them names them.
_SOURCE_OPTIONS = ("--aws", "--hetzner", "--ip")


class _HelpAction(argparse.Action):
    """Print HELP_TEXT and exit; argparse's own help is disabled."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        sys.stdout.write(HELP_TEXT)
        parser.exit()


def _explain(message: str) -> str:
    """Map an argparse diagnostic to a Chinese sentence; fall back to a generic one."""
    if "not allowed with argument" in message:
        first, second = sorted(set(re.findall(r"--[a-z-]+", message)), key=_SOURCE_OPTIONS.index)
        return f"{first} 与 {second} 不能同时使用"
    if "argument --to:" in message:
        return f"--to 只能是 {' 或 '.join(TO_FEATURES)}"
    if "argument --size:" in message:
        return "--size 要写规格，例如 2c4g"
    if "argument --hetzner:" in message:
        return f"--hetzner 的区域只能是 {'、'.join(HETZNER_REGIONS)}"
    if "invalid choice" in message and "--aws" in message:
        from cnsr.state import LEGACY_REGIONS

        word = _INVALID_CHOICE.search(message)
        if word and word[1] in LEGACY_REGIONS:
            return f"--aws 的区域 {word[1]} 已改为 {LEGACY_REGIONS[word[1]]}"
        return f"--aws 的区域只能是 {'、'.join(AWS_REGIONS)}"
    if word := _INVALID_CHOICE.search(message):
        return f"未知命令“{word[1]}”。命令写在最前面：部署服务器用 cnsr up <别名>，删除服务器用 cnsr rm <别名>"
    if "invalid choice" in message:
        return "未知命令"
    if "required" in message:
        return "缺少必需的参数"
    return "命令行参数不正确"


class _ArgumentParser(argparse.ArgumentParser):
    """Report usage errors in Chinese instead of argparse's English text."""

    def error(self, message: str) -> NoReturn:
        print(f"错误：{_explain(message)}", file=sys.stderr)
        print("运行 cnsr -h 查看用法。", file=sys.stderr)
        raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="cnsr", add_help=False)
    parser.add_argument("-h", "--help", action=_HelpAction)
    parser.add_argument("--version", action="version", version=f"cnsr {__version__}")
    parser.set_defaults(alias=None, aws=None, hetzner=None, ip=None, static_ip=None, size=None, to=None)
    commands = parser.add_subparsers(dest="command")

    up = commands.add_parser("up", add_help=False)
    up.add_argument("-h", "--help", action=_HelpAction)
    up.add_argument("alias", nargs="?")
    source = up.add_mutually_exclusive_group()
    source.add_argument("--aws", choices=AWS_REGIONS)
    source.add_argument("--hetzner", choices=HETZNER_REGIONS)
    source.add_argument("--ip")
    up.add_argument("--static-ip")
    up.add_argument("--size")
    up.add_argument("--to", choices=TO_FEATURES)

    rm = commands.add_parser("rm", add_help=False)
    rm.add_argument("-h", "--help", action=_HelpAction)
    rm.add_argument("alias", nargs="?")

    listing = commands.add_parser("list", add_help=False)
    listing.add_argument("-h", "--help", action=_HelpAction)
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def _use_utf8_output() -> None:
    """Windows consoles default to a legacy code page that cannot print Chinese."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _dispatch(ns: argparse.Namespace) -> int:
    # Imported lazily so that help and version never load the command modules.
    if ns.command == "up":
        from cnsr.commands import up

        return up.run(ns)
    if ns.command == "list":
        from cnsr.commands import listing

        return listing.run(ns)
    from cnsr.commands import rm

    return rm.run(ns)


@contextlib.contextmanager
def _hangup_interrupts() -> Iterator[None]:
    """Turn SIGHUP (a closed terminal) and SIGTERM into Ctrl-C while a command runs, so that its
    record is saved and a first deployment cleans up instead of leaving a billed instance."""
    signals = [getattr(signal, name) for name in ("SIGHUP", "SIGTERM") if hasattr(signal, name)]

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    before = {signum: signal.signal(signum, interrupt) for signum in signals}
    try:
        yield
    finally:
        for signum, handler in before.items():
            signal.signal(signum, handler)


def _run_logged(ns: argparse.Namespace, argv: list[str]) -> int:
    """Run a command inside a run log; the last line printed names the log file."""
    logs_dir = paths.config_dir(os.environ, sys.platform, Path.home()) / runlog.LOGS_DIR_NAME
    log = logging.getLogger(f"{runlog.LOGGER_NAME}.cli")
    code = 130
    run: runlog.Run | None = None  # stays None when Ctrl-C comes before the log is set up
    try:
        with _hangup_interrupts(), runlog.started(logs_dir, ns.command, ns.alias, argv) as run:
            try:
                code = _dispatch(ns)
            except CnsrError as exc:
                log.error("%s", exc)
                print(f"错误：{exc}", file=sys.stderr)
                code = 1
            except Exception as exc:  # a bug or an error nothing expected: still say where the log is
                log.error("意外错误", exc_info=exc)
                print(f"错误：意外错误（{type(exc).__name__}：{exc}），详情见日志", file=sys.stderr)
                code = 1
            run.exit_code = code
    except KeyboardInterrupt:
        print("已中断。修好问题后重跑同一条命令即可继续。", file=sys.stderr)
    if run is not None:
        _report_log(run)
    return code


def _report_log(run: runlog.Run) -> None:
    if run.path is None:
        print(f"警告：{run.warning}", file=sys.stderr, flush=True)
    else:
        print(f"日志：{run.path.resolve()}", flush=True)


def _menu_command() -> str | None:
    """The command picked in the top-level menu; None without a terminal to show it in."""
    if sys.platform == "win32":
        return None
    from cnsr.menu import terminal_select  # prompt_toolkit loads only for the menu

    select = terminal_select()
    if select is None:
        return None
    return MENU_COMMANDS[select("要做什么？", MENU_LABELS)]


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    argv = sys.argv[1:] if argv is None else argv
    try:
        ns = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    if ns.command is None:
        ns.command = _menu_command()
        if ns.command is None:
            sys.stdout.write(HELP_TEXT)
            return 0
    # Windows support is paused until its test failures are worked through.
    if sys.platform == "win32":
        sys.stderr.write("cnsr 暂不支持 Windows，请在 macOS 或 Linux 上运行。\n")
        return 1
    return _run_logged(ns, argv)
