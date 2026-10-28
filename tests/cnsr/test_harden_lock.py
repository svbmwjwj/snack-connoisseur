"""The hardening scripts run one at a time on a server: a rerun after a dropped connection may
find the first run still going there, and a rollback must not overlap the staging it undoes."""

from __future__ import annotations

import sys

import pytest
from harden_harness import harden_tool, server  # noqa: F401  (fixtures)

from cnsr.server.harden import stage_args
from cnsr.server.xray import PORTS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

LOCK = "flock -w 60 9"
RUNS = {
    "harden-stage.sh": stage_args(23456, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample cnsr", PORTS),
    "harden-cut.sh": ["23456"],
    "harden-revert.sh": ["all"],
}


@pytest.mark.parametrize("script", RUNS)
def test_each_script_takes_the_lock_first(server, script):
    assert server.run(script, *RUNS[script]).locked


@pytest.mark.parametrize("script", RUNS)
def test_a_script_still_running_holds_the_next_one_off(server, script):
    server.answer(LOCK, status=1)
    result = server.run(script, *RUNS[script])
    assert result.status == 1 and result.locked and result.calls == []
    assert "仍在运行" in result.stderr
