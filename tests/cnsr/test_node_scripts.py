"""Every server script bundled with cnsr must at least parse under bash."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

NODE_SCRIPTS = Path(__file__).resolve().parents[2] / "src" / "cnsr" / "assets" / "node"
# /bin/bash is 3.2 on macOS, the oldest bash the scripts must parse under.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")
def test_every_node_script_parses():
    scripts = sorted(NODE_SCRIPTS.glob("*.sh"))
    assert scripts, f"no server scripts found in {NODE_SCRIPTS}"
    failures = []
    for script in scripts:
        done = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            failures.append(f"{script.name}: {done.stderr.strip()}")
    assert failures == []
