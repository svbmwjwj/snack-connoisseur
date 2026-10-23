"""A private, secret-free log file for each add or rm run."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import cnsr
from cnsr import localconf
from cnsr.errors import CnsrError

LOGGER_NAME = "cnsr"
KEEP_LOGS = 20
LOGS_DIR_NAME = "logs"
MIN_SECRET_LENGTH = 6
MASK = "***"

_current: Run | None = None


def log_name(command: str, alias: str | None, started: datetime) -> str:
    """File name `<command>-<alias>_<YYYYMMDD-HHMMSS>.log`; the alias part is left out when unknown."""
    head = f"{command}-{_safe(alias)}" if alias else command
    return f"{head}_{started:%Y%m%d-%H%M%S}.log"


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", text)


def secret(value: str | None) -> None:
    """Register a value the current run must never write to its log; no-op without a run."""
    if _current is not None and value is not None and len(value) >= MIN_SECRET_LENGTH:
        _current.secrets.add(value)


def current() -> Run | None:
    return _current


def alias_known(alias: str) -> None:
    """Name the current run's file after the node once it is known, if it began without one."""
    if _current is not None and _current.alias is None:
        _current.rename_for(alias)


class _Masker(logging.Filter):
    """Replace registered secrets in each record's message and traceback with ***."""

    def __init__(self, secrets: set[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def mask(self, text: str) -> str:
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(value, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.mask(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.mask(record.exc_text)
        if record.stack_info:
            record.stack_info = self.mask(record.stack_info)
        return True


class _RunHandler(logging.FileHandler):
    """A log file that, once a write fails, stops writing and tells its run instead of printing.

    logging's default prints a traceback to stderr for every failed record, over the live display.
    """

    def __init__(self, path: Path, broken: Callable[[BaseException | None], None]) -> None:
        super().__init__(path, mode="a", encoding="utf-8")
        self._broken = broken
        self._failed = False

    def emit(self, record: logging.LogRecord) -> None:
        if not self._failed:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self._failed = True
        self._broken(sys.exc_info()[1])


class Run:
    """One command invocation and its log file; `path` is None when no log could be written."""

    def __init__(self, logs_dir: Path, command: str, alias: str | None, begun: datetime) -> None:
        self.logs_dir = logs_dir
        self.command = command
        self.alias = alias
        self.begun = begun
        self.path: Path | None = None
        self.exit_code: int | None = None
        self.warning: str | None = None
        self.secrets: set[str] = set()
        self._masker = _Masker(self.secrets)
        self._handler: logging.Handler = logging.NullHandler()
        self._restrict: Callable[[Path], None] = localconf.restrict_to_user

    def _open_handler(self) -> None:
        handler = _RunHandler(self.path, self._break)
        handler.setFormatter(_formatter())
        handler.addFilter(self._masker)
        self._handler = handler

    def _break(self, exc: BaseException | None) -> None:
        """Give up on the log after a write failed: the command goes on, the end warns once."""
        if self.path is not None:
            self.warning = f"无法继续写入运行日志（{self.path}）：{exc}"
            self.path = None

    def _close_handler(self) -> None:
        try:
            self._handler.close()
        except OSError as exc:
            self._break(exc)

    def _create_file(self, restrict: Callable[[Path], None], alias: str | None) -> Path:
        """Create the log exclusively with mode 600, adding -2, -3 ... to the name on a clash."""
        stem = log_name(self.command, alias, self.begun).removesuffix(".log")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        for n in range(1, 1000):
            path = self.logs_dir / f"{stem}{'' if n == 1 else f'-{n}'}.log"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            try:
                restrict(path)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return path
        raise FileExistsError(f"too many log files named {stem}")

    def rename_for(self, alias: str) -> None:
        """Rename the log after `alias`; the file is closed around the rename, as Windows needs.

        If the rename cannot be done the run keeps logging to the old name and the alias
        stays unset, so a later call tries again.
        """
        if self.path is None:
            self.alias = alias
            return
        logger = logging.getLogger(LOGGER_NAME)
        logger.removeHandler(self._handler)
        self._close_handler()
        if self.path is None:  # the log broke: nothing more is written, so nothing to rename
            self.alias, self._handler = alias, logging.NullHandler()
            return
        new: Path | None = None
        try:
            new = self._create_file(self._restrict, alias)
            os.replace(self.path, new)
            self.path, self.alias, new = new, alias, None
        except (OSError, CnsrError):
            pass
        finally:
            if new is not None:
                with contextlib.suppress(OSError):
                    new.unlink()
            try:
                self._open_handler()
            except OSError as exc:
                self.warning = f"无法继续写入运行日志（{self.path}）：{exc}"
                self.path = None
                self._handler = logging.NullHandler()
            logger.addHandler(self._handler)

    def _prune(self) -> None:
        try:
            logs = sorted(self.logs_dir.glob("*.log"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        except OSError:
            return
        keep = {self.path}
        for old in [p for p in logs if p not in keep][max(KEEP_LOGS - len(keep), 0) :]:
            with contextlib.suppress(OSError):
                old.unlink()


def _formatter() -> logging.Formatter:
    return logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")


@contextlib.contextmanager
def started(
    logs_dir: Path,
    command: str,
    alias: str | None,
    argv: list[str],
    *,
    now: Callable[[], datetime] = datetime.now,
    restrict: Callable[[Path], None] | None = None,
) -> Iterator[Run]:
    """Log everything the `cnsr` logger receives to a new file in `logs_dir` for the duration.

    A logs directory that cannot be written never stops the command: `Run.path` is
    None and `Run.warning` says why.
    """
    global _current
    run = Run(logs_dir, command, alias, now())
    if restrict is not None:
        run._restrict = restrict
    try:
        run.path = run._create_file(run._restrict, alias)
        run._open_handler()
    except (OSError, CnsrError) as exc:
        if run.path is not None:
            with contextlib.suppress(OSError):
                run.path.unlink()
        run.path = None
        run.warning = f"无法写入运行日志（{logs_dir}）：{exc}"
        run._handler = logging.NullHandler()

    logger = logging.getLogger(LOGGER_NAME)
    saved = (logger.level, logger.propagate, _current)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(run._handler)
    _current = run
    clock = time.monotonic()
    log = logging.getLogger(f"{LOGGER_NAME}.run")
    try:
        log.info("cnsr %s，命令 %s，参数 %s，Python %s，%s", cnsr.__version__, command, argv, sys.version.split()[0], sys.platform)
        try:
            yield run
        except KeyboardInterrupt:
            if run.exit_code is None:
                run.exit_code = 130
            log.info("已中断，退出码 %s，耗时 %.1f 秒", run.exit_code, time.monotonic() - clock)
            raise
        except SystemExit as exc:
            if run.exit_code is None:
                run.exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            log.info("退出码 %s，耗时 %.1f 秒", run.exit_code, time.monotonic() - clock)
            raise
        except BaseException as exc:
            log.error("未处理的异常", exc_info=exc)
            log.error("异常退出 %s，耗时 %.1f 秒", type(exc).__name__, time.monotonic() - clock)
            raise
        else:
            if run.exit_code is None:
                run.exit_code = 0
            log.info("退出码 %s，耗时 %.1f 秒", run.exit_code, time.monotonic() - clock)
    finally:
        logger.removeHandler(run._handler)
        run._close_handler()
        logger.setLevel(saved[0])
        logger.propagate = saved[1]
        _current = saved[2]
        if run.path is not None:
            run._prune()
