import logging
import os
import re
import stat
import sys
from datetime import datetime

import pytest

import cnsr
from cnsr import runlog

T0 = datetime(2026, 10, 18, 15, 30, 12)


def _lines(run) -> list[str]:
    return run.path.read_text(encoding="utf-8").splitlines()


def test_log_name():
    assert runlog.log_name("add", "cnsr-test-1", T0) == "add-cnsr-test-1_20261018-153012.log"
    assert runlog.log_name("add", None, T0) == "add_20261018-153012.log"


def test_lines_carry_time_and_level(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        logging.getLogger("cnsr.x").info("你好")
    pattern = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} INFO +你好$")
    assert any(pattern.match(line) for line in _lines(run))


def test_header_and_footer(tmp_path):
    with runlog.started(tmp_path, "add", "jp-1", ["add", "jp-1"], now=lambda: T0) as run:
        run.exit_code = 1
    lines = _lines(run)
    head = "\n".join(lines[:5])
    assert cnsr.__version__ in head
    assert "['add', 'jp-1']" in head
    assert sys.version.split()[0] in head
    assert "退出码 1" in lines[-1]
    assert "耗时" in lines[-1]


def test_registered_secrets_never_reach_the_file(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        runlog.secret("tok-1234567890")
        runlog.secret("jp")
        runlog.secret(None)
        logging.getLogger("cnsr.x").info("a tok-1234567890 b")
        logging.getLogger("cnsr.x").info("jp stays")
        logging.getLogger("cnsr.x").info("n=%s", "tok-1234567890")
    text = run.path.read_text(encoding="utf-8")
    assert "a *** b" in text
    assert "tok-1234567890" not in text
    assert "jp stays" in text


def test_longer_secret_is_replaced_before_its_prefix(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        runlog.secret("abcdef")
        runlog.secret("abcdefghij")
        logging.getLogger("cnsr.x").info("v abcdefghij w")
    assert "v *** w" in run.path.read_text(encoding="utf-8")


def test_secrets_in_tracebacks_are_masked(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        runlog.secret("tok-1234567890")
        try:
            raise ValueError("bad tok-1234567890")
        except ValueError:
            logging.getLogger("cnsr.x").exception("failed")
    assert "tok-1234567890" not in run.path.read_text(encoding="utf-8")


def test_secret_without_a_run_is_ignored():
    assert runlog.current() is None
    runlog.secret("x" * 10)
    runlog.alias_known("jp-1")


@pytest.mark.skipif(sys.platform == "win32", reason="posix permissions")
def test_file_is_private(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        pass
    assert stat.S_IMODE(run.path.stat().st_mode) == 0o600


def test_restrict_is_called_with_the_log_path(tmp_path):
    calls = []
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0, restrict=calls.append) as run:
        pass
    assert calls == [run.path]


def test_only_the_newest_20_are_kept(tmp_path):
    for i in range(25):
        old = tmp_path / f"old-{i:02d}.log"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (1_000_000_000 + i, 1_000_000_000 + i))
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        pass
    logs = sorted(p.name for p in tmp_path.glob("*.log"))
    assert len(logs) == runlog.KEEP_LOGS
    assert run.path.name in logs
    assert (tmp_path / "notes.txt").exists()
    assert "old-00.log" not in logs and "old-05.log" not in logs
    assert "old-24.log" in logs and "old-06.log" in logs


def test_same_second_runs_get_distinct_files(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as first:
        pass
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as second:
        pass
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()
    assert second.path.name == "add_20261018-153012-2.log"


def test_alias_known_renames_the_file(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        logging.getLogger("cnsr.x").info("before")
        old = run.path
        runlog.alias_known("jp-1")
        assert run.path.name.startswith("add-jp-1_")
        assert not old.exists()
        logging.getLogger("cnsr.x").info("after")
        runlog.alias_known("other")
        assert run.path.name.startswith("add-jp-1_")
    text = run.path.read_text(encoding="utf-8")
    assert "before" in text and "after" in text
    assert [p.name for p in tmp_path.glob("*.log")] == [run.path.name]


def test_alias_known_keeps_the_name_when_the_run_started_with_one(tmp_path):
    with runlog.started(tmp_path, "rm", "jp-1", ["rm", "jp-1"], now=lambda: T0) as run:
        before = run.path
        runlog.alias_known("jp-2")
        assert run.path == before


def test_unwritable_logs_dir_does_not_stop_the_command(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    with runlog.started(blocker, "add", None, ["add"], now=lambda: T0) as run:
        logging.getLogger("cnsr.x").info("still fine")
        logging.getLogger("cnsr.x").error("also fine")
        runlog.secret("x" * 10)
        runlog.alias_known("jp-1")
    assert run.path is None
    assert run.warning


def test_failing_restrict_means_no_log_file(tmp_path):
    def broken(path):
        raise OSError("denied")

    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0, restrict=broken) as run:
        pass
    assert run.path is None
    assert list(tmp_path.glob("*.log")) == []


def test_logger_is_released_after_the_run(tmp_path):
    logger = logging.getLogger(runlog.LOGGER_NAME)
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0):
        assert runlog.current() is not None
        assert logger.propagate is False
    assert runlog.current() is None
    assert logger.handlers == handlers
    assert logger.level == level
    assert logger.propagate is True


def test_interrupted_run_still_writes_the_footer(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
            raise KeyboardInterrupt
    assert "已中断" in _lines(run)[-1]
    assert runlog.current() is None


def test_other_exceptions_are_named_in_the_footer_and_propagate(tmp_path):
    with pytest.raises(RuntimeError):
        with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
            raise RuntimeError("boom")
    assert "RuntimeError" in _lines(run)[-1]
    assert runlog.current() is None


def test_system_exit_records_its_code(tmp_path):
    with pytest.raises(SystemExit):
        with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
            raise SystemExit(2)
    assert run.exit_code == 2
    assert "退出码 2" in _lines(run)[-1]


def test_clean_exit_without_a_code_is_zero(tmp_path):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        pass
    assert "退出码 0" in _lines(run)[-1]


def test_failing_restrict_during_rename_keeps_the_old_file_and_logging(tmp_path):
    from cnsr.errors import CnsrError

    calls = []

    def restrict(path):
        calls.append(path)
        if len(calls) > 1:
            raise CnsrError("icacls failed")

    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0, restrict=restrict) as run:
        old = run.path
        runlog.alias_known("jp-1")
        assert run.path == old
        assert run.alias is None
        logging.getLogger("cnsr.x").info("after failed rename")
    assert [p.name for p in tmp_path.glob("*.log")] == [old.name]
    assert "after failed rename" in old.read_text(encoding="utf-8")


def test_failed_replace_leaves_no_placeholder(tmp_path, monkeypatch):
    def broken_replace(src, dst):
        raise PermissionError("busy")

    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        old = run.path
        monkeypatch.setattr(runlog.os, "replace", broken_replace)
        runlog.alias_known("jp-1")
        monkeypatch.undo()
        logging.getLogger("cnsr.x").info("still here")
        assert run.path == old
    assert [p.name for p in tmp_path.glob("*.log")] == [old.name]
    assert "still here" in old.read_text(encoding="utf-8")


def test_handler_open_failure_leaves_no_empty_file(tmp_path, monkeypatch):
    def broken_open(self):
        raise OSError("disk full")

    monkeypatch.setattr(runlog.Run, "_open_handler", broken_open)
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        logging.getLogger("cnsr.x").info("fine")
    assert run.path is None and run.warning
    assert list(tmp_path.glob("*.log")) == []


class _FullDisk:
    """A file stream whose writes are buffered but never reach the disk: ENOSPC on flush."""

    def write(self, text):
        return len(text)

    def flush(self):
        raise OSError(28, "No space left on device")

    def close(self):
        self.flush()


class _FailsOnClose(_FullDisk):
    """Writes and flushes fine; only closing the file fails (as a full disk can on some systems)."""

    def flush(self):
        pass

    def close(self):
        raise OSError(28, "No space left on device")


def test_a_failing_log_write_stops_logging_quietly_and_the_rename_goes_on(tmp_path, capsys):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        old = run.path
        run._handler.stream.close()
        run._handler.stream = _FullDisk()
        logging.getLogger("cnsr.x").info("lost")
        logging.getLogger("cnsr.x").info("lost too")
        runlog.alias_known("jp-1")
        logging.getLogger("cnsr.x").info("after the rename")
    assert capsys.readouterr().err == ""
    assert run.path is None
    assert run.warning == f"无法继续写入运行日志（{old}）：[Errno 28] No space left on device"
    assert run.alias == "jp-1"
    assert "after the rename" not in old.read_text(encoding="utf-8")


def test_a_log_that_fails_only_when_closed_does_not_break_the_run_exit(tmp_path, capsys):
    with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
        old = run.path
        run._handler.stream.close()
        run._handler.stream = _FailsOnClose()
    assert capsys.readouterr().err == ""
    assert run.path is None
    assert run.warning == f"无法继续写入运行日志（{old}）：[Errno 28] No space left on device"
    assert runlog.current() is None
    assert logging.getLogger(runlog.LOGGER_NAME).handlers == []


def test_the_interrupted_footer_names_exit_code_130(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with runlog.started(tmp_path, "add", None, ["add"], now=lambda: T0) as run:
            raise KeyboardInterrupt
    assert re.search(r"已中断，退出码 130，耗时 \d+\.\d 秒$", _lines(run)[-1])
    assert run.exit_code == 130
