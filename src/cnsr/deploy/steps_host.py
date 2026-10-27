"""Pipeline steps 1-3: the node's address, the first login with the takeover check, and hardening."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from cnsr.deploy import reach, static_ip
from cnsr.deploy.context import DeployContext
from cnsr.deploy.dns_records import ensure_node_records
from cnsr.errors import CnsrError
from cnsr.paths import private_dir
from cnsr.server import baseline
from cnsr.server.harden import ADMIN, DROP_IN, Hardener
from cnsr.server.preflight import NODE_DIR, Facts, evaluate, parse_facts
from cnsr.ssh.config import HostBlock, read_block, write_block
from cnsr.ssh.remote import SshError
from cnsr.waiting import Deadline

STATIC_IP_SUFFIX = "-ip"
LOGIN_TIMEOUT = 60
# A new Lightsail instance counts as running well before its sshd takes the key.
BOOT_WAIT_SECONDS = 180
RETRY_SECONDS = 10
HOSTKEY_OPTIONS = ["删除旧指纹并重试", "停止"]
FREE_IP_OPTIONS = ["把它绑到这台节点的实例上使用", "停止"]
# Writing the new login to the control plane after hardening: tries, and the pause between them.
SAVE_ATTEMPTS = 3
SAVE_RETRY_SECONDS = 3


def step_ip(ctx: DeployContext) -> None:
    """Know the node's IPv4: the given one, or a Lightsail instance's (with its IPv6).

    A vendor server's IPv6 comes from preflight in ``step_connect``.
    """
    if ctx.req.source == "vps":
        ctx.node.ipv4 = ctx.req.ip
    else:
        _ensure_instance(ctx)
    ctx.save()


def _ensure_instance(ctx: DeployContext) -> None:
    node, lightsail = ctx.node, ctx.lightsail
    name = node.instance_name or node.alias
    ours = False  # a static IP cnsr allocates, or a free one you agreed to hand over
    if node.static_ip_name is None and ctx.req.static_ip:
        ip_name = static_ip.adopt(ctx, name)
    else:
        ip_name = node.static_ip_name or f"{node.alias}{STATIC_IP_SUFFIX}"
        if node.static_ip_name is None:  # cnsr's own IP from here on, released by rm
            node.static_ip_reused, node.static_ip_from = False, None
            ours = _confirm_free_static_ip(ctx, ip_name, name)
    ctx.deps.ui.detail(f"准备 Lightsail 实例 {name}（区域 {ctx.req.region}，已有就沿用）。")
    key_pair = lightsail.ensure_key_pair(node.ssh.key_name, ctx.key.line)
    # On record before it exists: a run killed outright right after the create leaves rm a name.
    node.instance_name = name
    ctx.save()
    # Also an instance the record names that was deleted by hand and is created again now.
    ctx.new_instance = lightsail.ensure_instance(name, key_pair)
    lightsail.wait_running(name)
    if node.static_ip_reused:
        static_ip.take(ctx, name)
    if ours:
        # Recorded before: an IP allocated whose attach then fails is still released by the cleanup.
        node.static_ip_name = ip_name
    ipv4 = lightsail.ensure_static_ip(name, ip_name)
    node.static_ip_name = ip_name
    node.ipv6 = lightsail.instance_ipv6(name)
    if ctx.new_instance:
        # A new instance has a new host key: forget what this machine remembers for its names.
        for host in (ipv4, node.domain):
            if host:
                ctx.deps.forget_host_key(host, node.ssh.port)
    node.ipv4 = ipv4


def _confirm_free_static_ip(ctx: DeployContext, ip_name: str, instance: str) -> bool:
    """Ask before a free static IP of the node's name, which the record does not name, is used.

    Static IPs cannot be tagged, so a free one that cnsr did not allocate in this run may be
    the user's own. Asked before the instance exists, so that stopping creates nothing. True
    when the IP becomes cnsr's: it does not exist yet, or it is free and you agreed.
    """
    exists, attached_to = ctx.lightsail.static_ip_attachment(ip_name)
    if not exists:
        return True
    if attached_to is not None:  # ensure_static_ip decides
        return False
    question = (
        f"Lightsail 里已有一个没有绑定任何实例的静态 IP {ip_name}，不是这次运行申请的，可能是你自己的。"
        f"要把它绑到实例 {instance} 上作为节点地址吗？以后运行 cnsr rm {ctx.node.alias} 会释放它。"
    )
    try:
        choice = ctx.deps.ui.choose(question, FREE_IP_OPTIONS)
    except CnsrError:
        raise CnsrError(
            f"Lightsail 里已有没有绑定实例的静态 IP {ip_name}，cnsr 不会在没有确认时接管它。"
            "请在终端里重跑同一条命令并确认，或换一个别名。"
        ) from None
    if choice != 0:
        raise CnsrError(f"已停止，静态 IP {ip_name} 保持原样。可以换一个别名，或在 Lightsail 控制台释放它后重跑。")
    return True


def step_connect(ctx: DeployContext) -> None:
    """Log in, refuse a server that is not fresh, then write the SSH alias and the domain records.

    Until the takeover gate passes nothing is written beyond an empty ``~/.ssh/config`` when
    there was none: ssh gets every setting from its ``-o`` options, the alias stays as it is
    and a vendor server gets no node record. The gate only
    guards the first takeover: right after a clean preflight ``/opt/cnsr`` is created, and
    later runs only read the facts.
    """
    node = ctx.node
    _ensure_ssh_config(ctx.deps.ssh_config)
    installed = _login(ctx)
    remote = ctx.remote()
    marker_found = ctx.exists(NODE_DIR)
    ctx.facts = parse_facts(remote.run_script("preflight.sh", [str(node.ssh.port)]))
    if not marker_found:
        findings = evaluate(ctx.facts, ssh_port=node.ssh.port, will_harden=_hardens(ctx), app_ports=ctx.app_ports)
        if findings:
            raise CnsrError(_refusal(findings, installed))
        remote.run(["mkdir", "-p", "-m", "700", NODE_DIR], sudo=True)
    ctx.taken_over = True
    if ctx.req.source == "vps":
        node.ipv6 = ctx.facts.ipv6 or None
    write_alias(ctx)
    ensure_node_records(ctx.cf, ctx.zone_id, ctx.zone, node, ctx.deps.rng)


def step_harden(ctx: DeployContext) -> None:
    """AWS: check the fixed baseline, then the cloud firewall. A vendor server reached as root:
    move SSH to admin on a random port. One cnsr hardened before: finish what an earlier run
    left undone. A vendor server reached as another user: nothing."""
    if ctx.req.source == "aws":
        _aws_baseline(ctx)
    elif ctx.node.ssh.user == "root":
        _harden_vendor(ctx)
    elif ctx.node.ssh.user == ADMIN and ctx.exists(DROP_IN):
        if not _hardener(ctx).resume(ctx.node.ssh.port):
            ctx.deps.ui.detail("这台服务器已经由 cnsr 加固过，设置无误，跳过加固。")
    else:
        ctx.deps.ui.detail(f"登录用户是 {ctx.node.ssh.user}，不是 root，说明这台服务器已经配置过，跳过加固。")


def write_alias(ctx: DeployContext) -> None:
    """Write the node's SSH alias with the current user and port.

    It points at the IP, unless it already points at the node's domain.
    """
    node, ssh_config = ctx.node, ctx.deps.ssh_config
    block = read_block(ssh_config, node.alias)
    on_domain = block is not None and bool(node.domain) and block.hostname == node.domain
    identity = str(ctx.identity_path)
    if block is not None and block.identity_file and Path(block.identity_file).expanduser() == ctx.identity_path:
        identity = block.identity_file  # the same file, written the user's way (for example with ~)
    hostname = node.domain if on_domain else node.ipv4
    write_block(ssh_config, HostBlock(node.alias, hostname, node.ssh.user, node.ssh.port, identity, ctx.agent))


def _ensure_ssh_config(path: Path) -> None:
    """Create an empty, private ``~/.ssh/config`` when there is none, since ``ssh -F`` needs one."""
    if path.exists() or path.is_symlink():
        return
    private_dir(path.parent)
    path.touch(mode=0o600)
    if sys.platform != "win32":
        path.chmod(0o600)


def _refusal(findings: list[str], key_installed: bool) -> str:
    listed = "\n".join(f"— {finding}" for finding in findings)
    unchanged = "没有建解析，没有写 SSH 别名，服务器上的服务和设置也没有动"
    if key_installed:
        unchanged += "；只是为了登录，在 root 的 ~/.ssh/authorized_keys 里加了你的公钥"
    return f"这台服务器不像全新系统，cnsr 只接管全新的服务器，已停止：\n{listed}\n{unchanged}。"


def _hardens(ctx: DeployContext) -> bool:
    return ctx.req.source == "vps" and ctx.node.ssh.user == "root"


def _login(ctx: DeployContext) -> bool:
    """Make sure a key login works; True when the key had to be installed with a password.

    When the recorded login is refused, the login the node's own alias names is tried once
    (see ``_adopt_alias_login``). A changed host key is forgotten only after you agree. A
    vendor server reached as root gets the key through one password login (OpenSSH asks for
    the password, cnsr never sees it), followed by a key login. An instance created in this
    run gets a few minutes to start sshd, whatever ssh says in the meantime.
    """
    deps = ctx.deps
    forgot = installed = tried_alias = False
    boot: Deadline | None = None  # from the first failure that may be a booting sshd
    verdict: reach.Verdict | None = None
    announced: set[str] = set()  # addresses the outside check was announced for
    answering = 0  # diagnoses that found the SSH port answering by then: retried at once
    while True:
        target = ctx.target()
        try:
            deps.make_remote(target).run(["true"], timeout=LOGIN_TIMEOUT)
            return installed
        except SshError as err:
            if err.kind in ("port", "key") and not tried_alias:
                tried_alias = True
                if _adopt_alias_login(ctx):
                    return installed
            if err.kind == "port" and err.unreachable:
                host = ctx.node.ipv4 or target.hostname
                verdict = reach.diagnose(ctx, host, target.port, announce=host not in announced)
                announced.add(host)
                if verdict.kind == "answering" and answering < 3:
                    answering += 1
                    continue
                if verdict.kind == "blocked" and reach.can_swap(ctx):
                    reach.swap_ip(ctx)
                    boot = verdict = None  # the new address gets the whole boot wait, and a verdict of its own
                    continue
                if verdict.kind in ("proxy", "path", "blocked"):
                    raise reach.explain(ctx, verdict, err) from err
            if err.kind == "hostkey" and not forgot:
                _forget_host_key(ctx, err)
                forgot = True
            elif err.kind in ("key", "password") and _hardens(ctx) and not installed:
                with deps.ui.handover():  # OpenSSH asks for the password on the terminal itself
                    deps.ui.info(  # inside, so it shows below the stage's context line
                        f"服务器还没有装入你的公钥。接下来 ssh 会提示输入 {target.hostname} 的 root 密码，"
                        "用来装入公钥；密码由 OpenSSH 自己读取，cnsr 不经手、不保存。"
                    )
                    deps.install_key(target, ctx.key.line, deps.ssh_config)
                deps.ui.detail("已用 root 密码登录一次，装入了公钥。")
                installed = True
            elif ctx.new_instance and err.kind in ("port", "key", "other") and (boot is None or not boot.passed()):
                # A booting sshd refuses, rejects the key or closes the connection mid-handshake.
                if boot is None:
                    boot = Deadline(BOOT_WAIT_SECONDS, deps.clock)
                    deps.ui.detail(f"实例刚启动，等待 SSH 就绪（最多 {BOOT_WAIT_SECONDS // 60} 分钟）。")
                deps.sleep(boot.step(RETRY_SECONDS))
            elif err.kind == "port" and verdict is not None:
                raise reach.explain(ctx, verdict, err) from err
            else:
                raise


def _adopt_alias_login(ctx: DeployContext) -> bool:
    """Log in once as the node's alias says, when that differs from the record; adopt it if it works.

    After hardening, the alias is written before the control plane hears of the new login,
    so the alias can be the newer of the two. Only an alias that points at this node (its IP
    or its domain) counts.
    """
    node = ctx.node
    block = read_block(ctx.deps.ssh_config, node.alias)
    hosts = {host for host in (node.ipv4, node.domain) if host}
    if block is None or not block.user or block.hostname not in hosts:
        return False
    if (block.user, block.port) == (node.ssh.user, node.ssh.port):
        return False
    target = dataclasses.replace(ctx.target(), user=block.user, port=block.port)
    try:
        ctx.deps.make_remote(target).run(["true"], timeout=LOGIN_TIMEOUT)
    except SshError:
        return False
    ctx.deps.ui.detail(
        f"记录里的登录方式（用户 {node.ssh.user}，端口 {node.ssh.port}）登录不上，"
        f"SSH 别名 {node.alias} 里的（用户 {block.user}，端口 {block.port}）可以，改用它。"
    )
    node.ssh.user, node.ssh.port = block.user, block.port
    ctx.save()
    return True


def _forget_host_key(ctx: DeployContext, err: SshError) -> None:
    target = ctx.target()
    choice = ctx.deps.ui.choose(
        f"服务器 {target.hostname} 的主机指纹和本机记录的不一致。如果它刚重装过系统，可以删除旧指纹后重试；"
        "如果没有重装过，可能有人在冒充它，请停止。",
        HOSTKEY_OPTIONS,
    )
    if choice != 0:
        raise err
    for host in (target.hostname, ctx.node.domain):
        if host:
            ctx.deps.forget_host_key(host, target.port)


def _aws_baseline(ctx: DeployContext) -> None:
    contexts = baseline.parse_contexts(ctx.remote().run_script("baseline-check.sh", []))
    problems = baseline.per_login(contexts)
    if problems:
        raise CnsrError("实例的 SSH 设置不符合基线，已停止：" + "；".join(problems))
    ctx.lightsail.set_firewall(ctx.node.instance_name, ctx.app_ports)
    *first, last = [22, *sorted({port for port, _ in ctx.app_ports})]
    ctx.deps.ui.detail(f"已开启自动安全更新；Lightsail 防火墙只放行 {'、'.join(map(str, first))} 和 {last}。")


def _hardener(ctx: DeployContext) -> Hardener:
    deps = ctx.deps

    def login_as(user: str, port: int):
        return deps.make_remote(dataclasses.replace(ctx.target(), user=user, port=port))

    return Hardener(ctx.remote(), login_as, deps.ui, deps.rng, sleep=deps.sleep)


def _harden_vendor(ctx: DeployContext) -> None:
    """Harden, then keep the new login: in the alias first (a local file), then in the control plane.

    The node takes the new login as soon as the cut is proven, so that a run stopped after
    that (Ctrl-C included) still stores it.
    """
    node = ctx.node

    def keep(user: str, port: int) -> None:
        node.ssh.user, node.ssh.port = user, port

    keep(*_hardener(ctx).run(ctx.key.line, _listening_ports(ctx.facts), on_cut=keep))
    try:
        write_alias(ctx)
    except CnsrError as exc:
        raise CnsrError(f"{exc}\n{_new_login(ctx)}") from exc
    _save_new_login(ctx)


def _new_login(ctx: DeployContext) -> str:
    ssh = ctx.node.ssh
    return f"加固已经完成：以后用用户 {ssh.user}、端口 {ssh.port} 登录 {ctx.node.ipv4}。"


def _save_new_login(ctx: DeployContext) -> None:
    """Store the node with its new login; a control plane that keeps failing ends the run.

    root and port 22 are closed now, so this is retried a few times before giving up.
    """
    alias = ctx.node.alias
    for attempt in range(1, SAVE_ATTEMPTS + 1):
        try:
            ctx.save()
            return
        except CnsrError as exc:
            if attempt == SAVE_ATTEMPTS:
                raise CnsrError(
                    f"没能把新的登录方式写入控制端（试了 {SAVE_ATTEMPTS} 次）：{exc}\n{_new_login(ctx)}"
                    f"SSH 别名 {alias} 已经改好；重跑 cnsr add {alias} 时会按别名登录并补写。"
                ) from exc
            ctx.deps.sleep(SAVE_RETRY_SECONDS)


def _listening_ports(facts: Facts | None) -> set[int]:
    ports = set()
    for address in facts.listen if facts else []:
        port = address.rpartition(":")[2]
        if port.isdecimal():
            ports.add(int(port))
    return ports
