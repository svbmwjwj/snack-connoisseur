"""Harness for running the bundled hardening scripts against a fake server.

Each script is fed to ``bash -s`` on stdin, as ``Remote.run_script`` does, with two changes to
its text so that it stays inside a temporary directory: absolute paths under /etc, /home, /opt,
/run and /usr/local/sbin point into ``Server.root`` instead, and the line that appends the system
sbin directories to PATH is dropped, so the only tools are the ones set up here. Every system
tool the scripts drive is a stand-in that logs its command line; plain file tools are real.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

NODE = Path(__file__).resolve().parents[2] / "src" / "cnsr" / "assets" / "node"
# /bin/bash is 3.2 on macOS, the oldest bash the scripts must run under.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"
LOCK_CALL = "flock -w 60 9"
PATH_LINE = 'PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"\n'
SYSTEM_PATH = re.compile(r"(?<![\w.$/-])/(?:etc|home|opt|run|usr/local/sbin)/")
REAL_TOOLS = ("bash", "base64", "cat", "chmod", "cp", "grep", "mkdir", "mv", "rm", "tail", "touch")
FAKE_TOOLS = (
    "apt-get", "chown", "dpkg-query", "flock", "getent", "id", "sshd", "systemctl", "systemd-run", "ufw", "useradd", "visudo",
)  # fmt: skip

# Every stand-in is this script under the tool's name. It appends its command line to
# calls.log and prints FAKE_DIR/fake/<tool>/<arguments>.out, with "/" in the arguments written
# as "%", spaces as "_" and the fake root left out. <arguments>.rc holds exit statuses, one per
# call, the last one repeating; without it the status is 0. Anything on its stdin is recorded.
FAKE_TOOL = r"""#!/bin/bash
tool=${0##*/}
[ -z "$(cat)" ] || : > "$FAKE_DIR/stdin-leak"
printf '%s\n' "$tool $*" >> "$FAKE_DIR/calls.log"
key="$*"
key=${key//"$FAKE_ROOT"/}
key=${key// /_}
key=${key//\//%}
base="$FAKE_DIR/fake/$tool/${key:-_}"
mkdir -p "${base%/*}"
count=0
[ ! -f "$base.count" ] || count=$(cat "$base.count")
echo $((count + 1)) > "$base.count"
[ ! -f "$base.out" ] || cat "$base.out"
status=0
if [ -f "$base.rc" ]; then
    codes=($(cat "$base.rc"))
    index=$count
    [ "$index" -lt "${#codes[@]}" ] || index=$((${#codes[@]} - 1))
    status=${codes[$index]}
fi
exit "$status"
"""


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str
    calls: list[str]  # without the lock every hardening script takes first
    stdin_leaked: bool
    locked: bool  # the script asked for the hardening lock first


class Server:
    """A fake Debian server: a root directory, stand-ins for its tools and their call log.

    It starts as a fresh server: no user admin and no group of that name, none of the packages
    installed, and SSH not socket-activated.
    """

    def __init__(self, base: Path, fake_tool: Path) -> None:
        self.base = base
        self.root = base / "root"
        self.bin = base / "bin"
        self.root.mkdir(parents=True)
        (self.root / "run").mkdir()
        self.bin.mkdir()
        for name in REAL_TOOLS:
            (self.bin / name).symlink_to(shutil.which(name))
        for name in FAKE_TOOLS:
            (self.bin / name).symlink_to(fake_tool)
        self.answer("id admin", status=1)
        self.answer("getent group admin", status=2)
        self.answer("systemctl is-active --quiet ssh.socket", status=3)

    def answer(self, command: str, stdout: str = "", status: int | str | None = None) -> None:
        """What the stand-in prints for ``command``; ``status`` may list one status per call."""
        tool, _, args = command.partition(" ")
        base = self.base / "fake" / tool / (args.replace(" ", "_").replace("/", "%") or "_")
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_name(base.name + ".out").write_text(stdout)
        if status is not None:
            base.with_name(base.name + ".rc").write_text(str(status))

    def remove_tool(self, name: str) -> None:
        (self.bin / name).unlink()

    def path(self, server_path: str) -> Path:
        return self.root / server_path.lstrip("/")

    def read(self, server_path: str) -> str:
        return self.path(server_path).read_text()

    def mode(self, server_path: str) -> int:
        return self.path(server_path).stat().st_mode & 0o777

    def write(self, server_path: str, content: str | bytes, mode: int = 0o644) -> None:
        path = self.path(server_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        path.chmod(mode)

    def leftovers(self) -> list[str]:
        """Temporary files the scripts left next to the files they replace."""
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if ".cnsr-" in p.name)

    def run(self, script: str, *args: str) -> Result:
        text = (NODE / script).read_text()
        assert text.count(PATH_LINE) == 1, "the harness expects exactly one PATH line"
        text = SYSTEM_PATH.sub(lambda match: f"{self.root}{match.group(0)}", text.replace(PATH_LINE, ""))
        log, leak = self.base / "calls.log", self.base / "stdin-leak"
        log.unlink(missing_ok=True)
        leak.unlink(missing_ok=True)
        env = {"PATH": str(self.bin), "FAKE_DIR": str(self.base), "FAKE_ROOT": str(self.root)}
        done = subprocess.run([BASH, "-s", "--", *args], input=text.encode(), capture_output=True, env=env, cwd=self.base, timeout=60)
        calls = log.read_text().replace(str(self.root), "").splitlines() if log.exists() else []
        locked = calls[:1] == [LOCK_CALL]
        calls = calls[1:] if locked else calls
        return Result(done.returncode, done.stdout.decode(), done.stderr.decode(), calls, leak.exists(), locked)


@pytest.fixture(scope="session")
def harden_tool(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("harden-tool") / "fake-tool"
    path.write_text(FAKE_TOOL)
    path.chmod(0o755)
    return path


@pytest.fixture
def server(tmp_path, harden_tool) -> Server:
    return Server(tmp_path / "server", harden_tool)
