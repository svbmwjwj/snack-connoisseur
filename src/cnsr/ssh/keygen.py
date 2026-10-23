"""A new key pair for one server, made by ssh-keygen, for someone without an agent key to use.

ssh-keygen and ssh-add own the terminal while they run: they ask for the passphrase themselves,
so cnsr never sees it, and cnsr never reads the private key.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cnsr.errors import CnsrError
from cnsr.ssh.tools import program

APPLE_SSH_ADD = "/usr/bin/ssh-add"


def generate(path: Path, comment: str, run=subprocess.run) -> None:
    """Make an ed25519 key pair at ``path`` and ``path.pub``; ssh-keygen asks for a passphrase."""
    done = run([program("ssh-keygen"), "-t", "ed25519", "-f", str(path), "-C", comment], check=False)
    if done.returncode != 0 or not path.exists():
        raise CnsrError(f"ssh-keygen 没能生成钥匙（退出码 {done.returncode}）。")


def has_passphrase(path: Path, run=subprocess.run) -> bool:
    """Whether the private key at ``path`` needs a passphrase (ssh-keygen cannot open it with none)."""
    done = run(
        [program("ssh-keygen"), "-y", "-P", "", "-f", str(path)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return done.returncode != 0


def add_to_agent(path: Path, platform: str = sys.platform, run=subprocess.run) -> None:
    """Hand the key to the session's ssh-agent; ssh-add asks for its passphrase once.

    On macOS the passphrase also goes into the login keychain, so the agent can take the key
    again after a restart without asking. That needs Apple's own ssh-add: one from Homebrew,
    earlier in PATH, does not know the option.
    """
    if platform == "darwin":
        args = [APPLE_SSH_ADD, "--apple-use-keychain", str(path)]
    else:
        args = [program("ssh-add"), str(path)]
    done = run(args, check=False)
    if done.returncode != 0:
        raise CnsrError(f"ssh-add 没能把钥匙交给 ssh-agent（退出码 {done.returncode}）。")
