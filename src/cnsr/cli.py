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
cnsr  自建 REALITY 节点：自动部署，SNI 失效时自动更换

用法  cnsr <命令> [参数]

命令
  add [别名]            添加一台节点；不带参数时逐项询问
      --aws <区域>      在 AWS Lightsail 新开一台（区域：jp、sg、us）
      --static-ip <IP>  配合 --aws：用你在这个区域已有的静态 IP 作节点地址；
                        它绑在别的实例上时先问你再改绑，cnsr rm 时解绑并保留
      --ip <IP>         接管一台已有服务器
  rm <别名>             删除节点；AWS 节点同时删除实例、释放静态 IP
  list                  列出现有节点、圈X 配置和 v2rayN 订阅地址

对已有别名再次运行 add：续跑没完成的步骤、补充备选 SNI，并把节点更新到当前版本。

选项
  -h, --help            显示本帮助
  --version             显示版本
"""

AWS_REGIONS = ("jp", "sg", "us")
# argparse: "argument command: invalid choice: 'jp-1' (choose from add, rm)"
_INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")


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
        return "--aws 与 --ip 不能同时使用"
    if "invalid choice" in message and "--aws" in message:
        return "--aws 的区域只能是 jp、sg、us"
    if word := _INVALID_CHOICE.search(message):
        return f"未知命令“{word[1]}”。命令写在最前面：添加节点用 cnsr add <别名>，删除节点用 cnsr rm <别名>"
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
    parser.set_defaults(alias=None, aws=None, ip=None, static_ip=None)
    commands = parser.add_subparsers(dest="command")

    add = commands.add_parser("add", add_help=False)
    add.add_argument("-h", "--help", action=_HelpAction)
    add.add_argument("alias", nargs="?")
    source = add.add_mutually_exclusive_group()
    source.add_argument("--aws", choices=AWS_REGIONS)
    source.add_argument("--ip")
    add.add_argument("--static-ip")

    rm = commands.add_parser("rm", add_help=False)
    rm.add_argument("-h", "--help", action=_HelpAction)
    rm.add_argument("alias")

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
    if ns.command == "add":
        from cnsr.commands import add

        return add.run(ns)
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


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    argv = sys.argv[1:] if argv is None else argv
    try:
        ns = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    if ns.command is None:
        sys.stdout.write(HELP_TEXT)
        return 0
    return _run_logged(ns, argv)
