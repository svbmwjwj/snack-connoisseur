"""Tests for choosing which OpenSSH programs cnsr runs."""

from __future__ import annotations

import pytest

from cnsr.ssh.tools import program

WINDOWS = {"SystemRoot": "C:\\Windows"}
SYSTEM_OPENSSH = "C:\\Windows\\System32\\OpenSSH"


@pytest.mark.parametrize("name", ["ssh", "ssh-add", "ssh-keygen"])
def test_windows_uses_its_own_openssh_when_it_is_installed(name):
    found = program(name, platform="win32", env=WINDOWS, isdir=lambda path: path == SYSTEM_OPENSSH)
    assert found == f"{SYSTEM_OPENSSH}\\{name}.exe"


def test_windows_without_its_own_openssh_searches_path():
    assert program("ssh", platform="win32", env=WINDOWS, isdir=lambda path: False) == "ssh"
    assert program("ssh", platform="win32", env={}, isdir=lambda path: True) == "ssh"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_other_systems_search_path(platform):
    assert program("ssh-add", platform=platform, env=WINDOWS, isdir=lambda path: True) == "ssh-add"
