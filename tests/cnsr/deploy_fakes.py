"""Test doubles for ``cnsr up``: the server, Lightsail, Hetzner, the terminal and the wiring into ``Deps``.

The ``fake_deps`` fixture in ``conftest.py`` builds ``DeployFakes`` with a Cloudflare account
(control plane, KV and the zone's DNS records) made of ``FakeCloudflare`` and ``FakeDnsZone``.
"""

from __future__ import annotations

import argparse
import base64
import logging
import random
import shlex
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from harden_fakes import NOT_LOADED, STOP_DENIED
from hetzner_fakes import FakeHetzner
from key_samples import ED, RSA
from lightsail_fakes import AWS_IP, AWS_IPV6, FakeLightsail
from terminal_fakes import Terminal

from cnsr import localconf
from cnsr.cloudflare.api import CloudflareClient
from cnsr.cloudflare.control import ProbeResult
from cnsr.commands import rm, up
from cnsr.features import ALL as ALL_FEATURES
from cnsr.deploy.context import Deps
from cnsr.errors import CnsrError
from cnsr.scan.egress import Egress
from cnsr.scan.ipv6_reach import Ipv6Reach
from cnsr.scan.probe import Hit
from cnsr.server.xray import XRAY_IMAGE
from cnsr.ssh.agent import SESSION_AGENT, AgentKey
from cnsr.ssh.remote import SshError, Target
from cnsr.state import Node


AGENT = "/home/u/.bitwarden-ssh-agent.sock"
VPS_IP = "198.51.100.7"
VPS_IPV6 = "2001:db8::7"
SSHD_CLEAN = "passwordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin no\n"


def agent_key(comment: str, sample: str = ED) -> AgentKey:
    """The test key ``sample`` as an agent lists it, with ``comment`` as its name."""
    kind, blob = sample.split()[:2]
    return AgentKey(f"{kind} {blob} {comment}", comment)


def preflight_output(*, listen=("0.0.0.0:22", "[::]:22"), ipv6: str = VPS_IPV6, user: str = "root") -> str:
    """What preflight.sh prints on a fresh Debian 12 server."""
    lines = ["os_id=debian", "os_version=12", "arch=x86_64", f"user={user}"]
    lines += [f"listen={address}" for address in listen]
    lines += ["containers=0", "admin_exists=0", f"ipv6={ipv6}"]
    return "\n".join(lines) + "\n"


class FakeServer:
    """One node as every ``Remote`` cnsr opens sees it: files, logins and canned script output.

    ``log`` holds ``(user, port, hostname, what)`` for every command, script and file write.
    ``fail_on`` maps a script name or a command's first two words to the error it raises;
    ``refused`` maps a login ``(user, port)`` to the kind of ``SshError`` it fails with.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.modes: dict[str, str] = {}
        self.dirs: set[str] = set()
        self.log: list[tuple[str, int, str, str]] = []
        self.fail_on: dict[str, BaseException] = {}
        self.fail_once: dict[str, BaseException] = {}  # like fail_on, for the first such call only
        self.refused: dict[tuple[str, int], str] = {}
        self.hostkey_stale = False
        self.key_installed = True
        self.preflight = preflight_output()
        self.sshd = "port 22\n" + SSHD_CLEAN
        self.sshd_root: str | None = None  # what `sshd -T -C user=root,...` shows when it differs
        self.timer, self.timer_stop_fails = "unloaded", False  # the transient revert timer, as in harden_fakes
        self.latency: dict[str, int] = {}  # domain -> ms; a domain missing here fails validation
        self.reality_fails: set[str] = set()  # domains a REALITY server cannot borrow (reality-candidates.sh)
        self.ufw_allowed: set[str] = set()  # rules `ufw allow` added, such as "443/tcp"
        self.script_args: dict[str, list[str]] = {}  # the last arguments each script ran with
        self.generated: list[str] = []  # private keys made by `xray x25519`
        self.verified: list[tuple[list[str], str, str]] = []  # verify-reality.sh: args, values file, its mode
        self.client_ip: Callable[[], str | None] = lambda: "203.0.113.5"  # SSH_CLIENT's address; None: unset
        self.chpasswd: list[str] = []  # what chpasswd read on stdin, once per run of it
        self.dirs.add("/etc/systemd/system")
        # cnsr-rotate.service: "inactive" until started; with ``service_fails`` it never comes up
        self.service, self.service_fails = "inactive", False
        # what `cnsr-rotate.sh check` prints, one per call, the last one repeating
        self.control_answers = ["200"]
        self.failed_attempt_seconds = 0.0
        self.blocked_hosts: set[str] = set()  # addresses this computer cannot reach at all
        self.dropped_ports: set[int] = set()  # ports this computer's network drops on any address
        self.erroring_ports: set[int] = set()  # ports a connection to fails with an OSError (no route)
        self.advance = lambda seconds: None  # set by DeployFakes to move its clock on
        self.control_checks = 0

    def what(self) -> list[str]:
        return [entry[3] for entry in self.log]

    def did(self, prefix: str) -> bool:
        return any(entry.startswith(prefix) for entry in self.what())

    def connect(self, target: Target, what: str) -> None:
        self.log.append((target.user, target.port, target.hostname, what))
        try:
            self._connect(target, what)
        except SshError:
            self.advance(self.failed_attempt_seconds)
            raise

    def _connect(self, target: Target, what: str) -> None:
        if target.hostname in self.blocked_hosts:
            raise SshError("port")
        if self.hostkey_stale:
            raise SshError("hostkey")
        if target.user == "root" and not self.key_installed:
            raise SshError("key")
        if (target.user, target.port) in self.refused:
            raise SshError(self.refused[(target.user, target.port)])
        error = self.fail_once.pop(what, None) or self.fail_on.get(what) or self.fail_on.get(" ".join(what.split()[:2]))
        if error is not None:
            raise error

    def script(self, name: str, args: list[str]) -> str:
        self.script_args[name] = args
        if name == "preflight.sh":
            return self.preflight
        if name == "baseline-check.sh":
            return f"cnsr-context admin\n{self.sshd}cnsr-context root\n{self.sshd_root or self.sshd}"
        if name == "validate-candidates.sh":
            pairs = zip(args[::2], args[1::2])
            return "".join(f"{d} {ip} {self.latency[d]}\n" for d, ip in pairs if d in self.latency)
        if name == "reality-candidates.sh":
            pairs = zip(args[::2], args[1::2])
            return "".join(f"{d} {ip}\n" for d, ip in pairs if d not in self.reality_fails)
        if name == "harden-stage.sh":
            self.sshd = f"port 22\nport {args[0]}\n" + SSHD_CLEAN
        if name == "harden-cut.sh":
            self.sshd, self.timer = f"port {args[0]}\n" + SSHD_CLEAN, "active"
        if name == "verify-reality.sh":  # reads the values file named by its argument, then deletes it
            self.verified.append((args, self.files.pop(args[0]), self.modes.pop(args[0])))
        if name in ("install-docker.sh", "verify-reality.sh", "harden-stage.sh", "harden-cut.sh", "harden-revert.sh"):
            return ""
        raise AssertionError(f"unexpected script: {name}")

    def command(self, command: list[str]) -> str:
        head = command[:2]
        if command == ["/opt/cnsr/bin/cnsr-rotate.sh", "check"]:
            assert self.service == "active", "checked the control plane before the service ran"
            answer = self.control_answers[min(self.control_checks, len(self.control_answers) - 1)]
            self.control_checks += 1
            return f"{answer}\n"
        if command[0] == "systemctl" and command[-1] == "cnsr-rotate.service":
            return self._rotate_service(command[1:-1])
        if command[:2] == ["systemctl", "stop"]:  # the revert timer: gone once it fired or was stopped
            if self.timer_stop_fails or self.timer != "active":
                raise CnsrError(STOP_DENIED if self.timer_stop_fails else NOT_LOADED)
            self.timer = "unloaded"
        if head == ["sh", "-c"] and "is-active" in command[2] and command[-1] == "cnsr-rotate.service":
            return f"{self.service}\n"  # `systemctl is-active ... || true`
        if head == ["sh", "-c"] and "is-active" in command[2]:
            return "active\n" if self.timer == "active" else "inactive\n"
        if command == ["true"] or command[0] in ("chpasswd", "systemctl"):
            return ""
        if head == ["ufw", "allow"]:
            self.ufw_allowed.add(command[2])
            return ""
        if head == ["sh", "-c"]:  # DeployContext.exists
            return "yes\n" if command[-1] in self.files or command[-1] in self.dirs else "no\n"
        if head == ["mkdir", "-p"] or head == ["install", "-d"]:
            self.dirs.update(arg for arg in command if arg.startswith("/"))
            return ""
        if head[0] == "cat":
            if command[1] not in self.files:
                raise CnsrError("远程命令执行失败（退出码 1）。")
            return self.files[command[1]]
        if head[0] == "touch":
            self.files[command[1]] = ""
            return ""
        if head == ["rm", "-f"]:
            for path in command[2:]:
                self.files.pop(path, None)
            return ""
        if head == ["mv", "-f"]:
            self.files[command[3]] = self.files.pop(command[2])
            self.modes[command[3]] = self.modes.pop(command[2], "")
            return ""
        if head == ["sshd", "-T"]:
            return self.sshd_root if self.sshd_root and "user=root," in command[-1] else self.sshd
        if command[:4] == ["docker", "run", "--rm", XRAY_IMAGE] and "x25519" in command:
            return self._x25519(command)
        if command[:3] == ["docker", "run", "--rm"] and "-test" in command:
            tested = command[-1].replace("/usr/local/etc/xray/", "/opt/cnsr/xray/conf/")
            assert tested in self.files, f"xray -test before {tested} was uploaded"
            if tested.rpartition(".")[2].lower() not in ("json", "jsonc", "toml", "yaml", "yml", "pb", "protobuf"):
                # Xray tells the format by the extension; it prints why on stdout and exits 23
                raise CnsrError(f"远程命令执行失败（退出码 23）：\nFailed to get format of {command[-1]}")
            return "Configuration OK.\n"
        if head == ["docker", "compose"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    def _rotate_service(self, verb: list[str]) -> str:
        """``systemctl enable --now``, ``start``, ``restart`` or ``stop`` of cnsr-rotate.service."""
        assert verb in (["enable", "--now"], ["start"], ["restart"], ["stop"]), f"unexpected systemctl {verb}"
        if verb == ["stop"]:
            assert "/etc/systemd/system/cnsr-rotate.service" in self.files, "stopped a service that is not installed"
            self.service = "inactive"
            return ""
        assert "/opt/cnsr/bin/cnsr-rotate.sh" in self.files, "the service started before its script was there"
        self.service = "activating" if self.service_fails else "active"
        return ""

    def _x25519(self, command: list[str]) -> str:
        assert "-i" not in command, "the private key must not appear on the node's command line"
        raw = bytes([len(self.generated) + 1]) * 32
        public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        private = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.generated.append(private)
        return f"PrivateKey: {private}\nPassword (PublicKey): {base64.urlsafe_b64encode(public).decode().rstrip('=')}\n"


class FakeNodeRemote:
    """Duck-typed ``Remote`` for one target on a ``FakeServer``."""

    def __init__(self, server: FakeServer, target: Target) -> None:
        self.server, self.target = server, target

    def run(self, command: list[str], *, input: str | None = None, sudo: bool = False, timeout: int = 600) -> str:
        target = self.target
        logging.getLogger("cnsr.ssh.remote").info(  # the line the real Remote logs for every command
            "[%s] %s@%s:%s 执行 %s", target.alias, target.user, target.hostname, target.port, shlex.join(command)
        )
        self.server.connect(self.target, " ".join(command))
        if command == ["printenv", "SSH_CLIENT"]:
            assert not sudo, "sudo resets the environment, and SSH_CLIENT with it"
            address = self.server.client_ip()
            if address is None:
                raise CnsrError("远程命令执行失败（退出码 1）。")
            return f"{address} 50022 22\n"
        if command == ["chpasswd"]:
            self.server.chpasswd.append(input or "")
        return self.server.command(command)

    def run_script(self, name: str, args: list[str], *, sudo: bool = True) -> str:
        self.server.connect(self.target, name)
        return self.server.script(name, list(args))

    def put_text(self, path: str, content: str, mode: str = "600") -> None:
        self.server.connect(self.target, f"put {path}")
        assert path.rsplit("/", 1)[0] in self.server.dirs, f"parent of {path} was not created"
        self.server.files[path], self.server.modes[path] = content, mode

    def can_login(self) -> bool:
        try:
            self.run(["true"])
        except SshError:
            return False
        return True


class DeployFakes:
    """Everything ``cnsr up`` talks to, wired into ``deps``; Cloudflare comes from the fixture.

    The attributes are read at call time, so a test changes behaviour by setting them.
    ``secrets.env`` starts with a Cloudflare token and zone; ``hcloud_env`` is what
    ``HCLOUD_TOKEN`` holds in the environment (None: unset). ``hetzner`` is every token's
    project, except the tokens ``other_projects`` maps to a Hetzner of their own.
    """

    def __init__(self, home: Path, cf_client: CloudflareClient, cloudflare, dns) -> None:
        self.home, self.cloudflare, self.dns = home, cloudflare, dns
        self.terminal = Terminal()
        self.server = FakeServer()
        self.lightsail = FakeLightsail()
        self.hetzner = FakeHetzner()
        self.hcloud_env: str | None = None
        self.other_projects: dict[str, FakeHetzner] = {}
        self.agents: dict[str | None, list[AgentKey]] = {AGENT: [agent_key("jp-1"), agent_key("other", RSA)]}
        # This computer's direct address and the one the node sees it come from. A list gives the
        # answer of each check in turn, its last one repeating.
        self.egress_result: Egress | list[Egress] = Egress("203.0.113.5", "203.0.113.5")
        self._seen = Egress("203.0.113.5", "203.0.113.5")
        self.server.client_ip = lambda: self._seen.node
        self.hits: list[Hit] | None = None  # None: twelve neighbours, site0 to site11
        self.probe_results: dict[str, list[ProbeResult]] = {}
        self.v6_results: list[Ipv6Reach] = []  # this computer's IPv6 tests in turn; then they pass
        self.public_dns = self.local_dns = self.aws_chain = True
        self.calls: dict[str, list] = defaultdict(list)
        self.passphrase = False  # whether the key ssh-keygen makes has one
        self.sleeps: list[float] = []
        # The clock the waits measure on: sleeps move it on, and so does every SSH attempt that
        # fails, by ``server.failed_attempt_seconds`` (ssh's own connect timeout, say).
        self.now = 0.0
        self.server.advance = self._advance
        self.server.latency = {f"site{i}.example.com": 20 + 5 * i for i in range(12)}
        self.deps = Deps(
            ui=self.terminal.ui(),
            conf_path=home / "cnsr" / "secrets.env",
            ssh_config=home / ".ssh" / "config",
            pub_dir=home / ".ssh" / "pub",
            make_cf=lambda token: self._record("make_cf", token, result=cf_client),
            make_lightsail=lambda region, keys: self._record("make_lightsail", region, keys, result=self.lightsail),
            make_remote=lambda target: FakeNodeRemote(self.server, target),
            detect_agents=lambda: list(self.agents),
            list_keys=lambda agent: list(self.agents.get(agent, [])),
            scan=self._scan,
            direct_address=self._direct_address,
            fake_ip_dns=lambda: self._seen.fake_dns,
            sni_ok=lambda ip, domain: self._record("sni_ok", ip, domain, result=True),
            probe=self._probe,
            check_control=lambda meta: None,
            rng=random.Random(7),
            sleep=self._sleep,
            clock=lambda: self.now,
            aws_default_credentials=lambda: self.aws_chain,
            make_hetzner=lambda region, token: self._record(
                "make_hetzner", region, token, result=self.other_projects.get(token, self.hetzner)
            ),
            hcloud_env_token=lambda: self.hcloud_env,
            forget_host_key=self._forget,
            install_key=self._install_key,
            public_resolves=lambda name, rtype, expected: self._record("public", name, rtype, expected, result=self.public_dns),
            local_resolves=lambda name, expected: self._record("local", name, expected, result=self.local_dns),
            probe_ipv6=self._probe_ipv6,
            key_dir=home / ".ssh" / "cnsr",
            generate_key=self._generate_key,
            key_has_passphrase=lambda path: self._record("has_passphrase", path, result=self.passphrase),
            add_key_to_agent=self._add_key_to_agent,
            local_tcp=self._local_tcp,
        )
        localconf.save(self.deps.conf_path, {"CLOUDFLARE_API_TOKEN": "t-secret", "CLOUDFLARE_ZONE": "example.com"})

    def _generate_key(self, path: Path, comment: str) -> None:
        """What ssh-keygen leaves: the private key and its public key, named ``comment``."""
        self.calls["generate_key"].append((path, comment))
        path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
        path.with_name(path.name + ".pub").write_text(agent_key(comment).line + "\n")

    def _add_key_to_agent(self, path: Path) -> None:
        self.calls["add_key_to_agent"].append(path)
        comment = path.name
        held = SESSION_AGENT if SESSION_AGENT in self.agents or None not in self.agents else None
        self.agents.setdefault(held, []).append(agent_key(comment))

    def _local_tcp(self, host: str, port: int) -> str:
        """A TCP connection from this computer: an address the server blocks times out."""
        self.calls["local_tcp"].append((host, port))
        dropped = host in self.server.blocked_hosts or port in self.server.dropped_ports
        if port in self.server.erroring_ports:
            return "error"
        return "timeout" if dropped else "refused"

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def _advance(self, seconds: float) -> None:
        self.now += seconds

    def node(self, alias: str = "jp-1") -> Node | None:
        """The node record the control plane's KV holds now."""
        for namespace_id, title in self.cloudflare.namespaces.items():
            raw = self.cloudflare.kv[namespace_id].get(f"node:{alias}") if title == "cnsr-control" else None
            if raw is not None:
                return Node.from_json(raw)
        return None

    def kv(self) -> dict[str, str]:
        """The control plane's KV namespace, key to value; changes go straight into it."""
        namespace_id = next(i for i, title in self.cloudflare.namespaces.items() if title == "cnsr-control")
        return self.cloudflare.kv[namespace_id]

    def store(self, node: Node) -> None:
        """Replace the node record in the control plane's KV."""
        self.kv()[f"node:{node.alias}"] = node.to_json()

    def _record(self, name: str, *args, result=None):
        self.calls[name].append(args)
        return result

    def _scan(self, ips: list[str], *, progress: Callable[[int, int], None] | None = None) -> list[Hit]:
        self.calls["scan"].append(ips)
        for done in range(1, len(ips) + 1) if progress is not None else ():
            progress(done, len(ips))
        if self.hits is not None:
            return self.hits
        return [Hit(f"203.0.113.{10 + i}", f"site{i}.example.com", "Example CA", False) for i in range(12)]

    def _direct_address(self) -> str | None:
        queued = self.egress_result
        if isinstance(queued, list):
            self._seen = queued.pop(0) if len(queued) > 1 else queued[0]
        else:
            self._seen = queued
        return self._record("egress", result=self._seen.domestic)

    def _probe(self, meta, host: str, port: int = 443) -> ProbeResult:
        key = host if port == 443 else f"{host}:{port}"
        self.calls["probe"].append(key)
        queued = self.probe_results.get(key)
        if not queued and port != 443 and self._sshd_down(port):  # closed for everyone, not just here
            return ProbeResult(ok=False, ms=None, error="连接被拒绝")
        return queued.pop(0) if queued else ProbeResult(ok=True, ms=12, error=None)

    def _sshd_down(self, port: int) -> bool:
        server = self.server
        down = server.fail_on.get("true")
        refused = any(kind == "port" for (_, refused_port), kind in server.refused.items() if refused_port == port)
        return refused or (isinstance(down, SshError) and down.kind == "port")

    def _probe_ipv6(self, host: str) -> Ipv6Reach:
        self.calls["probe_ipv6"].append(host)
        return self.v6_results.pop(0) if self.v6_results else Ipv6Reach(ok=True, ms=30, error=None)

    def _forget(self, hostname: str, port: int) -> None:
        self.calls["forget"].append((hostname, port))
        self.server.log.append(("", port, hostname, "ssh-keygen -R"))
        self.server.hostkey_stale = False

    def _install_key(self, target: Target, pubkey_line: str, ssh_config: Path) -> None:
        self.calls["install_key"].append((target.user, target.hostname, target.port, pubkey_line))
        self.server.key_installed = True


# ``size`` for ``run_up`` and ``up_args``: no ``--size`` with ``--aws``, so the size menu asks.
SIZE_MENU = object()


def run_up(
    fakes: DeployFakes,
    alias: str | None = "jp-1",
    *,
    aws: str | None = None,
    hetzner: str | None = None,
    ip: str | None = None,
    static_ip: str | None = None,
    size: str | None = None,
    features: list[str] | None = ALL_FEATURES,
    to: str | None = None,
) -> int:
    """Run ``cnsr up`` as the command line would, against ``fakes``.

    ``features`` stand in for the feature menu's answer; None shows the menu, or follows ``to``,
    the ``--to`` argument.
    """
    ns = up_args(alias, aws=aws, hetzner=hetzner, ip=ip, static_ip=static_ip, size=size, features=features, to=to)
    return up.run(ns, fakes.deps)


def up_args(alias: str | None = "jp-1", *, features: list[str] | None = ALL_FEATURES, **options) -> argparse.Namespace:
    """The arguments of ``cnsr up`` as the command line parses them; ``options`` are the flags given.

    ``--aws`` comes with ``--size 2c0.5g`` unless ``size`` is given; ``SIZE_MENU`` leaves it
    out, so that the size menu asks.
    """
    if options.get("aws") and options.get("size") is None:
        options["size"] = "2c0.5g"
    if options.get("size") is SIZE_MENU:
        options["size"] = None
    flags = {"aws": None, "hetzner": None, "ip": None, "static_ip": None, "size": None, "to": None, **options}
    return argparse.Namespace(command="up", alias=alias, features=features, **flags)


def run_rm(fakes: DeployFakes, alias: str = "jp-1") -> int:
    """Run ``cnsr rm`` as the command line would, against ``fakes``."""
    return rm.run(argparse.Namespace(command="rm", alias=alias), fakes.deps)
