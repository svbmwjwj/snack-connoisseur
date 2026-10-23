"""What one deployment works with: the request, the outside world (``Deps``) and the node so far."""

from __future__ import annotations

import os
import random
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import boto3
from botocore.exceptions import BotoCoreError

from cnsr import localconf, paths
from cnsr.aws.lightsail import REGIONS, Lightsail
from cnsr.cloudflare import control, dns
from cnsr.cloudflare.api import CloudflareClient
from cnsr.cloudflare.control import ProbeResult
from cnsr.errors import CnsrError
from cnsr.menu import terminal_select
from cnsr.progress import terminal_display
from cnsr.scan import egress, ipv6_reach, tcp_reach
from cnsr.scan import probe as tls_probe
from cnsr.scan.ipv6_reach import Ipv6Reach
from cnsr.scan.probe import Hit
from cnsr.scan.select import Candidate
from cnsr.server.preflight import Facts
from cnsr.ssh import agent, keygen, remote
from cnsr.ssh.agent import SESSION_AGENT, AgentKey
from cnsr.ssh.remote import Remote, Target
from cnsr.state import ControlMeta, Inventory, Node
from cnsr.ui import Ui

SECRETS_FILE = "secrets.env"
# Prints "yes" or "no", so that a failing ssh or sudo is never read as "the file is missing".
_EXISTS = 'if [ -e "$1" ]; then echo yes; else echo no; fi'


@dataclass
class AddRequest:
    """Where the node runs and how cnsr logs in to it, as gathered by ``cnsr add``."""

    alias: str
    source: Literal["aws", "vps"]
    region: str | None
    ip: str | None
    user: str
    port: int
    # The address of a static IP the user already has in the region (cnsr add --static-ip).
    static_ip: str | None = None


def _aws_default_credentials() -> bool:
    """True when boto3's default chain (environment, ~/.aws, ...) has credentials."""
    try:
        return boto3.Session().get_credentials() is not None
    except BotoCoreError:
        return False


def _make_lightsail(region_code: str, credentials: tuple[str, str] | None) -> Lightsail:
    """Lightsail for one region: with explicit keys when given, else boto3's default chain."""
    if credentials is None:
        return Lightsail(region_code)
    if region_code not in REGIONS:
        raise CnsrError(f"不支持的区域 {region_code}，可选：{'、'.join(REGIONS)}。")
    access_key, secret_key = credentials
    try:
        session = boto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        client = session.client("lightsail", region_name=REGIONS[region_code])
    except BotoCoreError as exc:
        raise CnsrError("无法用这组 AWS access key 创建 Lightsail 客户端，请检查 access key。") from exc
    return Lightsail(region_code, client=client)


@dataclass
class Deps:
    """Everything a command reaches outside the process. Tests pass fakes; see ``default_deps``.

    The fields after ``sleep`` are further seams to ssh-keygen, OpenSSH's password login,
    DNS, boto3's credential chain and this computer's IPv6 test of the node; their defaults
    are the real functions.
    """

    ui: Ui
    conf_path: Path
    ssh_config: Path
    pub_dir: Path
    make_cf: Callable[[str], CloudflareClient]
    make_lightsail: Callable[[str, tuple[str, str] | None], Lightsail]
    make_remote: Callable[[Target], Remote]
    detect_agents: Callable[[], list[str | None]]
    list_keys: Callable[[str | None], list[AgentKey]]
    scan: Callable[..., list[Hit]]  # tls_probe.scan: (ips, *, progress=None) -> hits
    direct_address: Callable[[], str | None]
    fake_ip_dns: Callable[[], bool]
    sni_ok: Callable[[str, str], bool]
    probe: Callable[..., ProbeResult]  # control.probe: (meta, host, port=443) -> result
    check_control: Callable[[ControlMeta], None]
    rng: random.Random
    sleep: Callable[[float], None]
    # What every wait measures its limit on (cnsr.waiting.Deadline).
    clock: Callable[[], float] = time.monotonic
    aws_default_credentials: Callable[[], bool] = _aws_default_credentials
    forget_host_key: Callable[[str, int], None] = remote.forget_host_key
    install_key: Callable[[Target, str, Path], None] = remote.install_key_with_password
    public_resolves: Callable[[str, str, set[str]], bool] = dns.public_resolves
    local_resolves: Callable[[str, set[str]], bool] = dns.local_resolves
    probe_ipv6: Callable[[str], Ipv6Reach] = ipv6_reach.probe_ipv6
    local_tcp: Callable[[str, int], str] = tcp_reach.connect
    key_dir: Path = field(default_factory=lambda: paths.key_dir(Path.home()))
    generate_key: Callable[[Path, str], None] = keygen.generate
    key_has_passphrase: Callable[[Path], bool] = keygen.has_passphrase
    add_key_to_agent: Callable[[Path], None] = keygen.add_to_agent


def known_lightsail(deps: Deps, region: str) -> Lightsail | None:
    """Lightsail for ``region`` from credentials already at hand, else None.

    boto3's default chain comes first, then the access key saved in ``secrets.env``.
    """
    if deps.aws_default_credentials():
        return deps.make_lightsail(region, None)
    values = localconf.load(deps.conf_path)
    saved = (values.get("AWS_ACCESS_KEY_ID"), values.get("AWS_SECRET_ACCESS_KEY"))
    if saved[0] and saved[1]:
        return deps.make_lightsail(region, saved)
    return None


def _detect_agents(env: Mapping[str, str], home: Path, platform: str) -> list[str | None]:
    """The usable agents, with the session's SSH_AUTH_SOCK socket named ``SESSION_AGENT``.

    That socket's path can change with every login, so it must never be stored as a path. A
    fixed socket (Bitwarden, 1Password) keeps its path even when SSH_AUTH_SOCK points at it.
    """
    session = env.get("SSH_AUTH_SOCK")
    fixed = set(agent.known_sockets(home, platform))
    found = agent.detect_agents(env, home, platform)
    return [SESSION_AGENT if session and path == session and path not in fixed else path for path in found]


def default_deps() -> Deps:
    """The real collaborators, for the current user on this machine."""
    home = Path.home()
    ssh_config = paths.ssh_config_path(home)
    return Deps(
        ui=Ui(select_fn=terminal_select(), display=terminal_display()),
        conf_path=paths.config_dir(os.environ, sys.platform, home) / SECRETS_FILE,
        ssh_config=ssh_config,
        pub_dir=paths.pub_dir(home),
        key_dir=paths.key_dir(home),
        make_cf=CloudflareClient,
        make_lightsail=_make_lightsail,
        make_remote=lambda target: Remote(target, ssh_config),
        detect_agents=lambda: _detect_agents(os.environ, home, sys.platform),
        list_keys=lambda name: agent.list_keys(agent.agent_socket(name, os.environ)),
        scan=tls_probe.scan,
        direct_address=egress.direct_address,
        fake_ip_dns=egress.fake_ip_dns,
        sni_ok=tls_probe.sni_ok,
        probe=control.probe,
        check_control=control.check_reachable,
        rng=secrets.SystemRandom(),
        sleep=time.sleep,
    )


@dataclass
class DeployContext:
    """One run of the pipeline. The steps read and update ``node``; ``save`` stores it in KV.

    The fields after ``agent`` carry results from one step to a later one.
    """

    req: AddRequest
    deps: Deps
    node: Node
    inventory: Inventory
    cf: CloudflareClient
    zone_id: str
    zone: str
    meta: ControlMeta
    key: AgentKey
    agent: str | None
    lightsail: Lightsail | None = None
    deployed: bool = False  # the node had a finished deployment before this run, so it is in use
    new_instance: bool = False  # step_ip created the Lightsail instance in this run (rerun included)
    taken_over: bool = False  # the takeover gate in step_connect passed in this run
    facts: Facts | None = None  # what preflight.sh printed in step_connect
    hits: list[Hit] | None = None  # scan results for step_sni; None when step_scan skipped the scan
    sni: Candidate | None = None  # the SNI step_sni chose for a node without an Xray config
    alias_switched: bool = False
    rotation_stopped: bool = False  # stop_rotation stopped the service in this run
    ip_swaps: int = 0  # static IPs this run released as blocked (reach.swap_ip)
    saved: str | None = field(default=None, repr=False)  # the node record as KV holds it

    @property
    def pub_path(self) -> Path:
        return self.deps.pub_dir / f"{self.node.ssh.key_name}.pub"

    @property
    def identity_path(self) -> Path:
        """The key file ssh is pointed at: the private key cnsr made for the node, else its public key.

        ssh finds a key with a passphrase in the agent by it, and reads one without straight from it.
        """
        made = self.deps.key_dir / self.node.ssh.key_name
        return made if made.exists() else self.pub_path

    def target(self, hostname: str | None = None) -> Target:
        """The node as ssh reaches it: by IP unless ``hostname`` says otherwise."""
        ssh = self.node.ssh
        identity = str(self.identity_path)
        return Target(self.node.alias, hostname or self.node.ipv4, ssh.user, ssh.port, identity, self.agent)

    def remote(self) -> Remote:
        return self.deps.make_remote(self.target())

    def exists(self, path: str) -> bool:
        """Whether ``path`` exists on the node."""
        return self.remote().run(["sh", "-c", _EXISTS, "sh", path], sudo=True).strip() == "yes"

    def read(self, path: str) -> str | None:
        """The text of the file ``path`` on the node, or None when there is none."""
        if not self.exists(path):
            return None
        return self.remote().run(["cat", path], sudo=True)

    def save(self) -> None:
        """Store the node in the control plane's KV when it changed since the last save.

        A vendor server is recorded only once the takeover gate has passed: before that cnsr
        has created nothing there. An AWS node is recorded from the start, since its instance
        and static IP cost money and ``rm`` must be able to find them: it is stored before the
        instance is created.
        """
        if self.req.source == "vps" and not self.taken_over:
            return
        text = self.node.to_json()
        if text != self.saved:
            self.inventory.put(self.node)
            self.saved = text
