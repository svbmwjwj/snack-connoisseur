"""A fake server for the Xray node scripts: harden_harness plus the tools these scripts drive.

``box`` is a ``harden_harness.Server`` that also has stand-ins for curl, docker, dpkg, ss and
sleep (they log their command line and print what ``answer`` set, see harden_harness) and the
real install, ls and mktemp. A script runs in ``bash -s`` with only these tools on its PATH.
"""

from __future__ import annotations

import shutil

import pytest
from harden_harness import Server, harden_tool  # noqa: F401  (fixture)

FAKE_TOOLS = ("curl", "docker", "dpkg", "ss", "sleep")
REAL_TOOLS = ("install", "ls", "mktemp")


@pytest.fixture
def box(tmp_path, harden_tool) -> Server:
    server = Server(tmp_path / "server", harden_tool)
    for name in FAKE_TOOLS:
        (server.bin / name).symlink_to(harden_tool)
    for name in REAL_TOOLS:
        (server.bin / name).symlink_to(shutil.which(name))
    return server
