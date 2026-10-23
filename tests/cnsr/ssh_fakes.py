"""Shared test doubles for the code that runs ssh."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from cnsr.ssh.remote import Remote, Target

T = Target("jp-1", "192.0.2.10", "root", 22, "~/.ssh/pub/jp-1.pub", None)
ADMIN = dataclasses.replace(T, user="admin")
CONFIG = Path("/home/u/.ssh/config")


class FakeSsh:
    """A stand-in for ``subprocess.run`` that records calls and answers with a canned result."""

    def __init__(self, returncode: int = 0, stdout: bytes | str = b"", stderr: bytes | str = b"", raises=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)

    @property
    def args(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def kwargs(self) -> dict:
        return self.calls[-1][1]


def run_on(target: Target, command: list[str] | None = None, *, fake: FakeSsh | None = None, **kwargs) -> FakeSsh:
    """Run ``command`` (default ``true``) on ``target`` through a ``FakeSsh`` and return the fake."""
    fake = fake or FakeSsh()
    Remote(target, CONFIG, run=fake).run(command or ["true"], **kwargs)
    return fake


def option_values(args: list[str]) -> list[str]:
    """Every value that follows a ``-o``."""
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-o"]


def option_values(args: list[str]) -> list[str]:
    """Every value that follows a ``-o``."""
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-o"]
