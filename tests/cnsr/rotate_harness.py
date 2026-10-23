"""Harness for running cnsr-rotate.sh against a fake node.

The script runs from a copy of its text without the line that appends the system sbin
directories to PATH, so the only tools are the ones set up here: the real bash, jq and plain
file tools, and stand-ins for curl and docker. ``CNSR_ROOT`` points the script's /opt/cnsr at
``Node.root``, where the stand-ins for validate-candidates.sh and verify-reality.sh live in bin/.

Every stand-in appends its command line to calls.log. curl answers from ``Node.respond``
(statuses and bodies per "METHOD /path" without the query, the last one repeating; status 0 is a request that
never reached the control plane) and records each request with the curl config it read on
stdin. docker records each ``run -test`` and the serverNames of config.json at each restart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "cnsr" / "assets" / "node" / "cnsr-rotate.sh"
# /bin/bash is 3.2 on macOS, the oldest bash the scripts must run under.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"
LIBS = ("rotate-control.sh", "rotate-switch.sh")
PATH_LINE = 'PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"\n'
REAL_TOOLS = ("bash", "cat", "chmod", "cp", "date", "mktemp", "mv", "rm", "sleep")

CONTROL = "https://cnsr.example.com"
ALIAS = "tokyo-1"
TOKEN = "nodeTokenAbc_123-xyz"
UUID = "11111111-1111-4111-8111-111111111111"
PUBLIC_KEY = "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE"
SHORT_ID = "0123456789abcdef"
OLD_SNI, OLD_IP = "old.example.com", "198.51.100.1"

FAKE_CURL = r"""#!{python}
import json, os, sys
fake = os.environ["FAKE_DIR"]
args = sys.argv[1:]
config = sys.stdin.read()
with open(os.path.join(fake, "calls.log"), "a") as log:
    log.write("curl " + " ".join(args) + "\n")
method = args[args.index("-X") + 1] if "-X" in args else "GET"
body = args[args.index("--data-binary") + 1] if "--data-binary" in args else None
url = args[-1]
key = method + " /" + url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
with open(os.path.join(fake, "requests.jsonl"), "a") as log:
    log.write(json.dumps({"key": key, "url": url, "body": body, "config": config, "args": args}) + "\n")
if os.path.exists(os.path.join(fake, "curl-blocks")):  # long enough to be interrupted
    open(os.path.join(fake, "curl-started"), "w").close()
    import time
    time.sleep(1)
with open(os.path.join(fake, "responses.json")) as f:
    answers = json.load(f).get(key, [[404, "not found"]])
counts_path = os.path.join(fake, "curl-counts.json")
counts = json.load(open(counts_path)) if os.path.exists(counts_path) else {}
index = min(counts.get(key, 0), len(answers) - 1)
counts[key] = counts.get(key, 0) + 1
json.dump(counts, open(counts_path, "w"))
status, text = answers[index]
if status == 0:
    sys.stdout.write("000")
    sys.exit(7)
with open(args[args.index("-o") + 1], "w") as out:
    out.write(text)
sys.stdout.write(str(status))
"""

FAKE_DOCKER = r"""#!{python}
import json, os, shutil, sys
fake = os.environ["FAKE_DIR"]
args = sys.argv[1:]
if sys.stdin.read():
    open(os.path.join(fake, "stdin-leak"), "w").close()
with open(os.path.join(fake, "calls.log"), "a") as log:
    log.write("docker " + " ".join(args) + "\n")
def status(name):
    path = os.path.join(fake, name)
    return int(open(path).read()) if os.path.exists(path) else 0
if args[:2] == ["run", "--rm"]:
    conf = args[args.index("-v") + 1].split(":")[0]
    staged = os.path.join(conf, args[-1].rsplit("/", 1)[1])
    with open(os.path.join(fake, "tested.jsonl"), "a") as log:
        log.write(json.dumps({"file": args[-1], "config": json.load(open(staged))}) + "\n")
    sys.exit(status("docker-test.rc"))
if args[0] == "compose" and args[-1] == "restart":
    config = json.load(open(os.path.join(os.environ["CNSR_ROOT"], "xray", "conf", "config.json")))
    names = config["inbounds"][0]["streamSettings"]["realitySettings"]["serverNames"]
    with open(os.path.join(fake, "restarts.jsonl"), "a") as log:
        log.write(json.dumps(names) + "\n")
    sys.exit(status("docker-restart.rc"))
"""

# Passes the pairs whose domain is listed in FAKE_DIR/valid; validate.rc makes it fail.
FAKE_VALIDATE = r"""#!/bin/bash
printf '%s\n' "validate-candidates.sh $*" >> "$FAKE_DIR/calls.log"
[ ! -f "$FAKE_DIR/validate.rc" ] || exit "$(cat "$FAKE_DIR/validate.rc")"
while [ "$#" -gt 0 ]; do
    if [ -f "$FAKE_DIR/valid" ] && grep -qx "$1" "$FAKE_DIR/valid"; then
        printf '%s %s 42\n' "$1" "$2"
    fi
    shift 2
done
"""

# Prints FAKE_DIR/china-<domain> (pass, blocked or unknown); "pass" when there is none.
FAKE_CHINA = r"""#!/bin/bash
printf '%s\n' "china-check.sh $*" >> "$FAKE_DIR/calls.log"
if [ -f "$FAKE_DIR/china-blocks" ]; then
    : > "$FAKE_DIR/china-started"
    sleep 1
fi
if [ -f "$FAKE_DIR/china-$1" ]; then cat "$FAKE_DIR/china-$1"; else echo pass; fi
"""

# Fails for the SNIs listed in FAKE_DIR/self-check-fails, and with the status in
# FAKE_DIR/verify.rc for every SNI (2: the check itself could not run). It keeps a copy of the values file
# and its mode, then deletes it as the real script does. With FAKE_DIR/verify-blocks it
# creates verify-started and takes a second, long enough to be interrupted.
FAKE_VERIFY = r"""#!/bin/bash
printf '%s\n' "verify-reality.sh $*" >> "$FAKE_DIR/calls.log"
if [ -f "$FAKE_DIR/verify-blocks" ]; then
    : > "$FAKE_DIR/verify-started"
    sleep 1
fi
ls -l "$1" | cut -c1-10 >> "$FAKE_DIR/verify-modes"
cat "$1" >> "$FAKE_DIR/verify-values"
sni=$(grep '^SNI=' "$1" | cut -d= -f2)
rm -f "$1"
[ ! -f "$FAKE_DIR/verify.rc" ] || exit "$(cat "$FAKE_DIR/verify.rc")"
if [ -f "$FAKE_DIR/self-check-fails" ] && grep -qx "$sni" "$FAKE_DIR/self-check-fails"; then
    exit 1
fi
"""


def xray_config(sni: str = OLD_SNI, ip: str = OLD_IP) -> dict:
    reality = {"dest": f"{ip}:443", "serverNames": [sni], "privateKey": "server-private-key", "shortIds": [SHORT_ID]}
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": ALIAS,
                "port": 443,
                "protocol": "vless",
                "settings": {"clients": [{"id": UUID, "flow": "xtls-rprx-vision"}], "decryption": "none"},
                "streamSettings": {"network": "raw", "security": "reality", "realitySettings": reality},
            }
        ],
        "outbounds": [{"protocol": "freedom"}],
    }


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str
    calls: list[str]


class Node:
    """A fake node with cnsr deployed: its files under ``root`` and its tools in ``bin``."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "opt-cnsr"
        self.bin = base / "bin"
        for directory in (self.root / "bin", self.root / "xray" / "conf", self.bin):
            directory.mkdir(parents=True)
        for name in (*REAL_TOOLS, "jq", "grep", "cut", "ls"):
            (self.bin / name).symlink_to(shutil.which(name))
        self._tool(self.bin / "curl", FAKE_CURL.replace("{python}", sys.executable))
        self._tool(self.bin / "docker", FAKE_DOCKER.replace("{python}", sys.executable))
        self._tool(self.root / "bin" / "validate-candidates.sh", FAKE_VALIDATE)
        self._tool(self.root / "bin" / "verify-reality.sh", FAKE_VERIFY)
        self._tool(self.root / "bin" / "china-check.sh", FAKE_CHINA)
        self.write("node.env", f"CNSR_CONTROL={CONTROL}\nCNSR_NODE_TOKEN={TOKEN}\n", 0o600)
        self.write("identity.env", f"ALIAS={ALIAS}\nUUID={UUID}\nPUBLIC_KEY={PUBLIC_KEY}\nSHORT_ID={SHORT_ID}\n", 0o600)
        self.write("xray/conf/config.json", json.dumps(xray_config(), indent=2) + "\n", 0o644)
        self.backups([])
        self.responses: dict[str, list[list]] = {}
        self.env: dict[str, str] = {}  # extra environment for the script
        self.respond("GET", "task", [200, '{"rotate":false}'])
        self.responses["GET /generate_204"] = [[204, ""]]  # the node's own way out works

    def offline(self) -> None:
        """The node's own connection to the internet is down."""
        self.responses["GET /generate_204"] = [[0, ""]]

    @staticmethod
    def _tool(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o755)

    def write(self, name: str, text: str, mode: int = 0o600) -> None:
        path = self.root / name
        path.write_text(text)
        path.chmod(mode)

    def read(self, name: str) -> str:
        return (self.root / name).read_text()

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()

    def mode(self, name: str) -> int:
        return (self.root / name).stat().st_mode & 0o777

    def backups(self, domains: list[tuple[str, str]]) -> None:
        entries = [{"domain": d, "ip": ip, "latency_ms": 40 + n} for n, (d, ip) in enumerate(domains)]
        self.write("backups.json", json.dumps(entries, indent=2) + "\n")

    def backup_domains(self) -> list[str]:
        return [entry["domain"] for entry in json.loads(self.read("backups.json"))]

    def config(self) -> dict:
        return json.loads(self.read("xray/conf/config.json"))

    def reality(self) -> dict:
        return self.config()["inbounds"][0]["streamSettings"]["realitySettings"]

    def respond(self, method: str, endpoint: str, *answers: list) -> None:
        """What the control plane answers, one [status, body] per call, the last one repeating."""
        self.responses[f"{method} /node/{ALIAS}/{endpoint}"] = [list(answer) for answer in answers]

    def valid(self, *domains: str) -> None:
        (self.base / "valid").write_text("".join(f"{d}\n" for d in domains))

    def china(self, domain: str, verdict: str) -> None:
        """What china-check.sh says about ``domain``."""
        (self.base / f"china-{domain}").write_text(verdict + "\n")

    def self_check_fails(self, *domains: str) -> None:
        (self.base / "self-check-fails").write_text("".join(f"{d}\n" for d in domains))

    def fail(self, name: str, status: int) -> None:
        (self.base / name).write_text(str(status))

    def requests(self) -> list[dict]:
        path = self.base / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def sent(self, method: str, endpoint: str) -> list:
        """The bodies of the requests to one endpoint, parsed."""
        key = f"{method} /node/{ALIAS}/{endpoint}"
        return [json.loads(r["body"]) if r["body"] else None for r in self.requests() if r["key"] == key]

    def jsonl(self, name: str) -> list:
        path = self.base / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def _prepare(self, args: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
        text = SCRIPT.read_text()
        assert text.count(PATH_LINE) == 1, "the harness expects exactly one PATH line"
        script = self.base / "cnsr-rotate.sh"
        script.write_text(text.replace(PATH_LINE, ""))
        for lib in LIBS:  # sourced from $BIN, which is root/bin here
            (self.root / "bin" / lib).write_text((SCRIPT.parent / lib).read_text())
        (self.base / "responses.json").write_text(json.dumps(self.responses))
        (self.base / "calls.log").unlink(missing_ok=True)
        env = {"PATH": str(self.bin), "FAKE_DIR": str(self.base), "CNSR_ROOT": str(self.root), **self.env}
        return [BASH, str(script), *args], env

    def calls(self) -> list[str]:
        log = self.base / "calls.log"
        return log.read_text().replace(str(self.root), "/opt/cnsr").splitlines() if log.exists() else []

    def run(self, *args: str, timeout: int = 60) -> Result:
        command, env = self._prepare(args)
        done = subprocess.run(command, capture_output=True, env=env, cwd=self.base, timeout=timeout, stdin=subprocess.DEVNULL)
        return Result(done.returncode, done.stdout.decode(), done.stderr.decode(), self.calls())

    def start(self, *args: str) -> subprocess.Popen:
        """Start the script without waiting for it."""
        command, env = self._prepare(args)
        return subprocess.Popen(
            command, env=env, cwd=self.base, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )


@pytest.fixture
def node(tmp_path) -> Node:
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed on this machine; the rotate script edits JSON with the system jq")
    return Node(tmp_path / "node")
