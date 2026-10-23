"""Which OpenSSH client programs cnsr runs: ssh, ssh-add and ssh-keygen."""

from __future__ import annotations

import ntpath
import os
import sys
from collections.abc import Callable, Mapping


def program(
    name: str,
    *,
    platform: str = sys.platform,
    env: Mapping[str, str] = os.environ,
    isdir: Callable[[str], bool] = os.path.isdir,
) -> str:
    """The command to run for the OpenSSH program ``name``.

    On Windows the system's own copy in ``%SystemRoot%\\System32\\OpenSSH`` is used when it is
    installed: Git for Windows puts its OpenSSH earlier in PATH, and that one cannot reach the
    Windows agent's named pipe. Everywhere else the name is looked up in PATH as usual.
    """
    root = env.get("SystemRoot") or env.get("SYSTEMROOT")
    if platform == "win32" and root:
        directory = ntpath.join(root, "System32", "OpenSSH")
        if isdir(directory):
            return ntpath.join(directory, f"{name}.exe")
    return name
