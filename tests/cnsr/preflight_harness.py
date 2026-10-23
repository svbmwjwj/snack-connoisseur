"""Harness for running the bundled preflight.sh against fake server tools.

The script is fed to ``bash -s`` on stdin as ``Remote.run_script`` does. ``PATH`` holds only
stand-ins for the tools it probes (plus ``grep``), so nothing here depends on the host and a
tool the script is not meant to use does not exist.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from preflight_samples import CLEAN_FAKES, DEBIAN_12

from cnsr.server.preflight import Facts, parse_facts

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "cnsr" / "assets" / "node" / "preflight.sh"
# /bin/bash is 3.2 on macOS, the oldest bash the script must run under.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"

# One stand-in for every probed tool, reached through symlinks named after the tool. What a
# tool prints for a given command line comes from FAKE_DIR/<tool>/<arguments joined by "_">
# (stdout), with optional ".rc" (exit status) and ".err" (stderr) files next to it. An
# unexpected command line exits 2. A tool that finds anything on its stdin, or a locale other
# than C, is recorded; so is the PATH it saw.
FAKE_TOOL = """#!/bin/bash
tool=${0##*/}
if [ "$tool" = timeout ]; then
    echo "$*" >> "$FAKE_DIR/timeout.log"
    shift
    exec "$@"
fi
[ -z "$(cat)" ] || : > "$FAKE_DIR/stdin-leak"
[ "${LC_ALL-}" = C ] || : > "$FAKE_DIR/locale-not-c"
echo "$PATH" > "$FAKE_DIR/path.log"
key=$*
key=${key// /_}
base="$FAKE_DIR/$tool/${key:-_}"
[ -f "$base" ] || exit 2
[ ! -f "$base.err" ] || cat "$base.err" >&2
cat "$base"
[ ! -f "$base.rc" ] || exit "$(cat "$base.rc")"
"""
@pytest.fixture(scope="session")
def fake_tool(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("fake-tool") / "fake-tool"
    path.write_text(FAKE_TOOL)
    path.chmod(0o755)
    return path


class Run:
    def __init__(self, done: subprocess.CompletedProcess, base: Path) -> None:
        self.done, self.base = done, base

    @property
    def lines(self) -> list[str]:
        return self.done.stdout.decode().removesuffix("\n").split("\n")  # not splitlines(): it would hide a stray CR

    @property
    def stdin_leaked(self) -> bool:
        return (self.base / "stdin-leak").exists()

    @property
    def locale_not_c(self) -> bool:
        return (self.base / "locale-not-c").exists()

    @property
    def path_seen(self) -> str:
        return (self.base / "path.log").read_text().strip()

    @property
    def time_limited(self) -> list[str]:
        log = self.base / "timeout.log"
        return log.read_text().splitlines() if log.exists() else []

    def facts(self, prefix: str) -> list[str]:
        return [line for line in self.lines if line.startswith(prefix)]


def run_script(tmp_path: Path, fake_tool: Path, fakes: dict | None = None, *, without: tuple[str, ...] = (),
               os_release: str | None = DEBIAN_12, sshd_config: str | None = "#Port 22\n", sshd_config_d: dict[str, str] | None = None,
               args: tuple[str, ...] = ("22",)) -> Run:
    """Run preflight.sh against the clean fake server, changed by ``fakes`` and ``without`` (tools to remove).

    A fake is ``"tool args": stdout`` or ``"tool args": (stdout, exit status[, stderr])``.
    ``sshd_config_d`` maps file names to contents; without it the drop-in directory does not exist.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    fakes = {**CLEAN_FAKES, **(fakes or {})}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("grep", "cat"):
        (bin_dir / name).symlink_to(shutil.which(name))
    for name in {command.split()[0] for command in fakes} | {"timeout"}:
        if name not in without:
            (bin_dir / name).symlink_to(fake_tool)
    for command, result in fakes.items():
        tool, *words = command.split()
        stdout, status, stderr = (result, 0, "") if isinstance(result, str) else (*result, "")[:3]
        base = tmp_path / tool / ("_".join(words) or "_")
        base.parent.mkdir(exist_ok=True)
        base.write_text(stdout)
        if status:
            base.with_name(base.name + ".rc").write_text(str(status))
        if stderr:
            base.with_name(base.name + ".err").write_text(stderr)
    env = {"PATH": str(bin_dir), "FAKE_DIR": str(tmp_path)}
    for variable, name, content in (("PREFLIGHT_OS_RELEASE", "os-release", os_release), ("PREFLIGHT_SSHD_CONFIG", "sshd_config", sshd_config)):
        env[variable] = str(tmp_path / name)
        if content is not None:
            (tmp_path / name).write_text(content)
    env["PREFLIGHT_SSHD_CONFIG_D"] = str(tmp_path / "sshd_config.d")
    if sshd_config_d is not None:
        (tmp_path / "sshd_config.d").mkdir()
        for name, content in sshd_config_d.items():
            (tmp_path / "sshd_config.d" / name).write_text(content)
    done = subprocess.run([BASH, "-s", "--", *args], input=SCRIPT.read_bytes(), capture_output=True, env=env, cwd=tmp_path, timeout=60)
    return Run(done, tmp_path)


@pytest.fixture
def run(tmp_path, fake_tool):
    """``run(fakes, ...)`` runs the script in a fresh directory; call it again for a second run."""
    count = 0

    def go(fakes: dict | None = None, **kwargs) -> Run:
        nonlocal count
        count += 1
        return run_script(tmp_path / f"run-{count}", fake_tool, fakes, **kwargs)

    return go


def facts_of(out: Run) -> Facts:
    return parse_facts("\n".join(out.lines))


def failing(*commands: str) -> dict:
    return {command: ("", 127, "boom\n") for command in commands}
