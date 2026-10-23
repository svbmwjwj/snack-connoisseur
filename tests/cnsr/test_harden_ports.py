"""The ufw rules cnsr opens for a new SSH port are remembered, so none is left open for good.

A stage interrupted before the cut and run again picks another random port: the rule for the
first one must go, and a full revert removes every port cnsr opened, not only the drop-in's.
"""

from __future__ import annotations

import sys

import pytest
from harden_harness import NODE, harden_tool, server  # noqa: F401  (fixtures)
from key_samples import ED as PUB

from cnsr.server.harden import stage_args

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

PORT, OLD = 23456, 11111
PORTS = "/opt/cnsr/.ufw-ports-by-cnsr"
REVERT = "/usr/local/sbin/cnsr-harden-revert"
DROP_IN = "/etc/ssh/sshd_config.d/10-cnsr.conf"


@pytest.fixture
def fresh(server):
    server.answer("sshd -T", f"port 22\nport {PORT}\npasswordauthentication yes\n")
    return server


def test_a_stage_remembers_the_port_it_opens(fresh):
    assert fresh.run("harden-stage.sh", *stage_args(PORT, PUB)).status == 0
    assert fresh.read(PORTS) == f"{PORT}\n"


def test_a_second_stage_closes_the_port_the_first_one_opened(fresh):
    fresh.write(PORTS, f"{OLD}\n")
    out = fresh.run("harden-stage.sh", *stage_args(PORT, PUB))
    assert out.status == 0
    assert out.calls.index(f"ufw allow {PORT}/tcp") < out.calls.index(f"ufw delete allow {OLD}/tcp")
    assert fresh.read(PORTS) == f"{PORT}\n"


def test_a_full_revert_closes_every_port_cnsr_opened(server):
    server.write(REVERT, (NODE / "harden-revert.sh").read_bytes(), mode=0o755)
    server.write(DROP_IN, f"Port 22\nPort {PORT}\n")
    server.write(PORTS, f"{OLD}\n{PORT}\n")
    out = server.run("harden-revert.sh", "all")
    assert out.status == 0
    deleted = [call for call in out.calls if call.startswith("ufw delete")]
    assert deleted == [f"ufw delete allow {PORT}/tcp", f"ufw delete allow {OLD}/tcp"]  # each once
    assert not server.path(PORTS).exists()
