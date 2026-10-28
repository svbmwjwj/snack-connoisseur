"""Pipeline steps 6-7: Xray on the node, then registering the node and moving its SSH alias."""

from __future__ import annotations

import secrets

from cnsr import runlog
from cnsr.cloudflare.control import ControlPlaneBlocked, ProbeResult
from cnsr.deploy.context import DeployContext
from cnsr.deploy.dns_records import drop_aaaa, publish_aaaa, switch_alias_when_ready
from cnsr.deploy.steps_rotate import put_service_input
from cnsr.errors import CnsrError
from cnsr.qx import node_line
from cnsr.scan.ipv6_reach import Ipv6Reach
from cnsr.server.xray import (
    COMPOSE_PATH,
    CONFIG_PATH,
    IDENTITY_ENV_PATH,
    NODE_DIR,
    NODE_ENV_PATH,
    VERIFY_ENV_PATH,
    XRAY_IMAGE,
    compose_text,
    config_template,
    derive_public_key,
    identity_env,
    new_identity,
    parse_x25519,
    read_identity,
    render_config,
    verify_env,
)
from cnsr.ssh.config import read_block
from cnsr.waiting import Deadline
from cnsr.ssh.remote import Remote, SshError
from cnsr.state import XrayInfo

XRAY_DIR = f"{NODE_DIR}/xray"
CONF_DIR = f"{XRAY_DIR}/conf"
# Exists from just before a changed config moves into place until Xray has been restarted.
RESTART_MARKER = f"{XRAY_DIR}/.restart-pending"
CONTAINER_CONF_DIR = "/usr/local/etc/xray"  # where compose.yml mounts CONF_DIR
# Xray tells a config file's format by its extension, so the staged copy must end in .json.
STAGED_NAME = "config.new.json"
X25519 = ["docker", "run", "--rm", XRAY_IMAGE, "x25519"]
COMPOSE = ["docker", "compose", "-f", COMPOSE_PATH]
RETRY_SECONDS = 10
# The first `up`, the key generation and the config test of a new image pull the Xray image.
PULL_TIMEOUT = 300
# A control plane deployed minutes ago may still be waiting for its certificate.
V4_PROBE_WAIT = 120
V6_PROBE_WAIT = 30
PUBLIC_DNS_WAIT = 120


def step_xray(ctx: DeployContext) -> None:
    """Install Docker, run Xray with the node's identity, and prove it works from both sides.

    An existing config keeps its UUID, keys, shortId and SNI; the files are rewritten from the
    current templates. A running Xray is restarted when its config changed, or when an earlier
    run changed it but stopped before the restart (``RESTART_MARKER``). The identity the SNI
    rotation service checks its handshakes with goes in ``IDENTITY_ENV_PATH``, without the
    private key.
    """
    node, ui = ctx.node, ctx.deps.ui
    remote = ctx.remote()
    restart = ctx.exists(RESTART_MARKER)
    ui.detail("安装 Docker（已装好就跳过）。")
    remote.run_script("install-docker.sh", [])
    current = ctx.read(CONFIG_PATH)
    info, private_key = _identity(ctx, remote, current)
    remote.run(["install", "-d", "-m", "755", XRAY_DIR, CONF_DIR], sudo=True)
    remote.put_text(COMPOSE_PATH, compose_text(), "644")
    config = render_config(
        config_template(),
        alias=node.alias,
        uuid=info.uuid,
        private_key=private_key,
        short_id=info.short_id,
        sni=info.sni,
        dest_ip=info.dest_ip,
    )
    if config != current:
        # A running Xray keeps the old config until it restarts.
        _install_config(remote, config, restart_owed=current is not None)
        restart = restart or current is not None
    node.xray = info
    put_service_input(ctx, IDENTITY_ENV_PATH, identity_env(node.alias, info.uuid, info.public_key, info.short_id))
    remote.run([*COMPOSE, "up", "-d"], sudo=True, timeout=PULL_TIMEOUT)
    if restart:
        remote.run([*COMPOSE, "restart"], sudo=True)
        remote.run(["rm", "-f", RESTART_MARKER], sudo=True)
    ui.detail("在节点本机验证 REALITY 握手。")
    # The values go in a root-only file, never on the command line, where sudo would log them.
    remote.put_text(VERIFY_ENV_PATH, verify_env(info.uuid, info.public_key, info.short_id, info.sni), "600")
    remote.run_script("verify-reality.sh", [VERIFY_ENV_PATH])
    _check_from_outside(ctx)


def step_register(ctx: DeployContext) -> None:
    """Publish the node's line once public DNS answers, give the node its token, move the alias."""
    node, deps = ctx.node, ctx.deps
    _wait_for_public_dns(ctx)
    node.node_token = node.node_token or secrets.token_urlsafe(24)
    runlog.secret(node.node_token)
    node.line = node_line(node.domain, node.xray, node.alias)
    # The nodes call the workers.dev address: see ensure_control_plane.
    env = f"CNSR_CONTROL=https://{ctx.meta.node_host}\nCNSR_NODE_TOKEN={node.node_token}\n"
    put_service_input(ctx, NODE_ENV_PATH, env)
    ctx.save()
    deps.ui.detail("节点已登记到控制端。")
    block = read_block(deps.ssh_config, node.alias)
    if block is None or block.hostname != node.domain:
        deps.ui.detail(f"确认本机能解析 {node.domain} 并用它登录后，把 SSH 别名切到域名（最多等 2 分钟）。")

    def can_login_via(hostname: str) -> bool:
        return deps.make_remote(ctx.target(hostname)).can_login()

    ctx.alias_switched = switch_alias_when_ready(
        node, deps.ssh_config, can_login_via, deps.local_resolves, sleep=deps.sleep, clock=deps.clock
    )


def _identity(ctx: DeployContext, remote: Remote, current: str | None) -> tuple[XrayInfo, str]:
    """The node's identity and private key: read from its config, or new when it has none.

    The public key of an existing config is computed here from its private key. When
    ``step_sni`` chose a new SNI for a node that already has a config, the rest is kept.
    """
    if current is not None:
        found = read_identity(current)
        public_key = derive_public_key(found["private_key"])
        if ctx.sni is None:
            ctx.deps.ui.detail("沿用节点上已有的 UUID、密钥、shortId 和 SNI。")
            sni, dest_ip = found["sni"], found["dest_ip"]
        else:
            ctx.deps.ui.detail("沿用节点上已有的 UUID、密钥和 shortId，换用新选定的 SNI。")
            sni, dest_ip = ctx.sni.domain, ctx.sni.ip
        info = XrayInfo(found["uuid"], public_key, found["short_id"], sni, dest_ip)
        runlog.secret(found["private_key"])
        return info, found["private_key"]
    if ctx.sni is None:
        raise CnsrError("还没有选定 SNI，无法生成 Xray 配置。重跑同一条命令即可。")
    private_key, public_key = parse_x25519(remote.run(X25519, sudo=True, timeout=PULL_TIMEOUT))
    runlog.secret(private_key)
    uuid, short_id = new_identity(ctx.deps.rng)
    return XrayInfo(uuid, public_key, short_id, ctx.sni.domain, ctx.sni.ip), private_key


def _install_config(remote: Remote, config: str, *, restart_owed: bool) -> None:
    """Put ``config`` in place only after ``xray run -test`` accepts it.

    With ``restart_owed`` the restart marker is created first, so that a run interrupted after
    the move still restarts Xray next time.
    """
    staged = f"{CONF_DIR}/{STAGED_NAME}"
    remote.put_text(staged, config, "644")
    mount = f"{CONF_DIR}:{CONTAINER_CONF_DIR}:ro"
    test = ["docker", "run", "--rm", "-v", mount, XRAY_IMAGE, "run", "-test", "-c", f"{CONTAINER_CONF_DIR}/{STAGED_NAME}"]
    try:
        remote.run(test, sudo=True, timeout=PULL_TIMEOUT)  # a new XRAY_IMAGE is pulled here
    except SshError:
        raise
    except CnsrError as exc:
        raise CnsrError(f"新的 Xray 配置没有通过 xray -test，节点上的配置保持不变：{exc}") from exc
    if restart_owed:
        remote.run(["touch", RESTART_MARKER], sudo=True)
    remote.run(["mv", "-f", staged, CONFIG_PATH], sudo=True)


def _check_from_outside(ctx: DeployContext) -> None:
    """Test 443 on each address from outside the node.

    The control plane tests IPv4, which must work. Its Worker cannot reach IPv6 addresses, so
    IPv6 is tested from this computer (``_check_ipv6``).
    """
    node, ui = ctx.node, ctx.deps.ui
    ui.detail("由控制端从外部测试节点 IPv4 的 443 端口。")
    v4 = _probe(ctx, node.ipv4, V4_PROBE_WAIT, "新部署的控制端可能还在签发证书")
    if not v4.control_reached:
        raise CnsrError(
            f"这台电脑连不上控制端 {ctx.meta.host}（{v4.error}），没能从外部测试节点，这不是节点的问题。"
            "请检查这台电脑的网络后重跑同一条命令即可继续。"
        )
    if not v4.ok:
        raise CnsrError(
            f"从外部测试节点 {node.ipv4} 的 443 端口没有成功（{v4.error}）。"
            "请检查云防火墙或厂商防火墙是否放行 443；修好后重跑同一条命令即可继续。"
        )
    if node.ipv6:
        _check_ipv6(ctx)


def _check_ipv6(ctx: DeployContext) -> None:
    """Keep, restore or drop the AAAA record by this computer's test of the node's IPv6 443.

    The record is restored only when the test passes, and deleted only when this computer
    reaches IPv6 and the node did not answer. A test that cannot be trusted (no IPv6 here, or
    a TUN proxy in the way) leaves the record as it is.
    """
    node, ui = ctx.node, ctx.deps.ui
    published = "AAAA" in node.dns_record_ids
    if ctx.deps.fake_ip_dns():
        _ipv6_unverified(ctx, "这台电脑的 DNS 返回代理的假地址（TUN 模式），连接会经过代理")
        return
    ui.detail("由这台电脑测试节点 IPv6 的 443 端口（控制端测不了 IPv6）。")
    v6 = _probe_ipv6(ctx)
    if not v6.tested:
        _ipv6_unverified(ctx, f"这台电脑没有可用的 IPv6 网络（{v6.error}）")
        return
    if v6.ok:
        if node.ipv6_probe_failed or not published:
            publish_aaaa(ctx.cf, ctx.zone_id, node)
            node.ipv6_probe_failed = False
        return
    node.ipv6, node.ipv6_probe_failed = None, True
    if not published:
        ui.detail(f"节点 IPv6 的 443 端口仍然测不通（{v6.error}），节点继续只走 IPv4。")
        return
    drop_aaaa(ctx.cf, ctx.zone_id, node)
    ui.warn(
        f"从这台电脑测试节点 IPv6 地址的 443 端口没有成功（{v6.error}），已删除 AAAA 记录，节点只走 IPv4。"
        "以后重跑时会再测，通过了才重新加上。"
    )


def _ipv6_unverified(ctx: DeployContext, why: str) -> None:
    """Say the node's IPv6 could not be tested; its AAAA record stays as it is."""
    node = ctx.node
    if "AAAA" in node.dns_record_ids:
        ctx.deps.ui.warn(f"{why}，没法验证节点的 IPv6，AAAA 记录保持不变。")
        return
    node.ipv6 = None  # it failed its last test, so the node goes on as IPv4 only
    ctx.deps.ui.warn(f"{why}，没法再测节点的 IPv6；它上次没有测通，暂不加回 AAAA 记录，节点继续只走 IPv4。")


def _probe_ipv6(ctx: DeployContext) -> Ipv6Reach:
    """Test the node's IPv6 from here every 10 seconds until it answers, this computer turns
    out to have no IPv6, or ``V6_PROBE_WAIT`` seconds have passed."""
    host, deadline = ctx.node.ipv6, Deadline(V6_PROBE_WAIT, ctx.deps.clock)
    while True:
        result = ctx.deps.probe_ipv6(host)
        ctx.deps.ui.detail(f"本机测试 {host} 的 443 端口：{_ipv6_text(result)}。")
        if result.ok or not result.tested or deadline.passed():
            return result
        ctx.deps.sleep(deadline.step(RETRY_SECONDS))


def _probe(ctx: DeployContext, host: str, wait: int, note: str) -> ProbeResult:
    """Probe ``host`` every 10 seconds until it answers or ``wait`` seconds have passed."""
    deadline = Deadline(wait, ctx.deps.clock)
    first = True
    while True:
        try:
            result = ctx.deps.probe(ctx.meta, host)
        except ControlPlaneBlocked:
            raise  # waiting does not lift a Cloudflare challenge
        except CnsrError as exc:
            ctx.deps.ui.detail(f"外部探测 {host} 的 443 端口：控制端没有给出结果（{exc}）。")
            if deadline.passed():
                raise
        else:
            ctx.deps.ui.detail(f"外部探测 {host} 的 443 端口：{_probe_text(result)}。")
            if result.ok or deadline.passed():
                return result
        if first:
            first = False
            ctx.deps.ui.detail(f"{host} 暂时没有测通（{note}），每 {RETRY_SECONDS} 秒重试一次，最多 {wait} 秒。")
        ctx.deps.sleep(deadline.step(RETRY_SECONDS))


def _wait_for_public_dns(ctx: DeployContext) -> None:
    """Wait until public DNS answers the node's addresses, so the line can name the domain."""
    node = ctx.node
    published_v6 = node.ipv6 and "AAAA" in node.dns_record_ids
    wanted = [("A", {node.ipv4})] + ([("AAAA", {node.ipv6})] if published_v6 else [])
    deadline = Deadline(PUBLIC_DNS_WAIT, ctx.deps.clock)
    while not _public_dns_ready(ctx, wanted):
        if deadline.passed():
            raise CnsrError(
                f"等了 {PUBLIC_DNS_WAIT} 秒，公共 DNS 仍没有返回 {node.domain} 的预期地址"
                "（也可能是这台电脑的 DNS 查询没有成功）。节点本身已经部署好，"
                f"稍后重跑 cnsr add {node.alias} 即可完成登记。"
            )
        ctx.deps.sleep(deadline.step(RETRY_SECONDS))


def _public_dns_ready(ctx: DeployContext, wanted: list[tuple[str, set[str]]]) -> bool:
    """Whether public DNS answers every wanted record; each check is logged."""
    domain = ctx.node.domain
    for rtype, expected in wanted:
        ok = ctx.deps.public_resolves(domain, rtype, expected)
        answer = "已返回" if ok else "还没有返回"
        ctx.deps.ui.detail(f"公共 DNS：{domain} 的 {rtype} 记录{answer}预期地址 {'、'.join(sorted(expected))}。")
        if not ok:
            return False
    return True


def _ipv6_text(result: Ipv6Reach) -> str:
    if result.ok:
        return f"连通，{result.ms} 毫秒" if result.ms is not None else "连通"
    if not result.tested:
        return f"没有测试（{result.error}）"
    return f"没有连通（{result.error}）"


def _probe_text(result: ProbeResult) -> str:
    if result.ok:
        return f"连通，{result.ms} 毫秒" if result.ms is not None else "连通"
    if not result.control_reached:
        return f"连不上控制端（{result.error}）"
    return f"没有连通（{result.error}）"
