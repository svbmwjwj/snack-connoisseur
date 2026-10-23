"""Pipeline steps 4-5: scan the node's neighbourhood from this computer, then let the node choose."""

from __future__ import annotations

from cnsr.deploy.context import DeployContext
from cnsr.deploy.steps_rotate import stop_rotation
from cnsr.errors import CnsrError
from cnsr.scan.egress import FAKE_IP_NETWORK, Egress, client_address, is_direct
from cnsr.scan.filters import shortlist
from cnsr.scan.ranges import scan_network, scan_targets
from cnsr.scan.select import Candidate, backups_json, choose, parse_backups, parse_validation
from cnsr.server.xray import BACKUPS_PATH, CONFIG_PATH, read_identity
from cnsr.ssh.remote import SshError

# Backups are checked again, from the node and from mainland China, right before each use, so
# every candidate that passed is kept. A rerun scans again only when fewer than REFILL_BELOW are left.
MAX_BACKUPS = 30
REFILL_BELOW = 10
UNKNOWN_EGRESS_OPTIONS = ["确认是直连，继续扫描", "停止"]
PROXIED_OPTIONS = ["已关闭代理，重新检查", "停止"]


def step_scan(ctx: DeployContext) -> None:
    """Find SNI candidates around the node; skipped when Xray runs and enough backups are left.

    The scan must leave this computer directly, never through a proxy. A first deployment
    waits until it does (see ``require_direct``); a node that already runs Xray only misses
    the refill of its backups.
    """
    ui = ctx.deps.ui
    ctx.hits = None
    configured = ctx.exists(CONFIG_PATH)
    left = len(_backups_on_node(ctx)) if configured else 0
    if configured and left >= REFILL_BELOW:
        ui.detail(f"节点已有 Xray 配置，还有 {left} 个备选 SNI（不少于 {REFILL_BELOW} 个），跳过扫描。")
        return
    network = scan_network(ctx.node.ipv4)
    if configured:
        seen = _egress(ctx)
        if is_direct(seen) is not True:
            ui.warn(
                f"备选未补充：{_route_text(seen, network)}，扫描不能经过代理。"
                "节点照常使用当前的 SNI 和备选；关闭代理后重跑即可补充。"
            )
            return
    else:
        require_direct(ctx)
    targets = scan_targets(ctx.node.ipv4)
    ui.detail(f"在你的电脑上扫描 {network} 里的 {len(targets)} 个地址（不含节点所在的 /22，只连 443 端口，不带 SNI）。")
    found = ctx.deps.scan(targets, progress=ui.stage_count)
    short = shortlist(found)
    hits = [hit for hit in short if ctx.deps.sni_ok(hit.ip, hit.domain)]
    ui.detail(f"扫到 {len(found)} 个站点，过滤后剩 {len(short)} 个，带 SNI 握手成功 {len(hits)} 个。")
    if hits:
        ctx.hits = hits
    elif configured:
        ui.warn("这次没有扫到可用的候选，备选 SNI 保持不变。")
    else:
        raise CnsrError("节点附近没有找到可用的 SNI 候选，已停止，不会盲选。")


def step_sni(ctx: DeployContext) -> None:
    """Have the node check the candidates itself, then keep the SNI and write the backups.

    A node that already runs Xray keeps its current SNI; only its backups are replaced. New
    backups end the control plane's "backups used up" mark for the node. An installed SNI
    rotation service is stopped first, as from here on the add changes the node's files.
    """
    ui = ctx.deps.ui
    stop_rotation(ctx)
    if ctx.hits is None:
        ui.detail("沿用节点上已有的 SNI 和备选。")
        return
    remote = ctx.remote()
    args = [value for hit in ctx.hits for value in (hit.domain, hit.ip)]
    ui.detail(f"交给节点验证的候选：{'、'.join(f'{hit.domain}（{hit.ip}）' for hit in ctx.hits)}")
    passed = parse_validation(remote.run_script("validate-candidates.sh", args))
    ui.detail(f"节点验证了 {len(ctx.hits)} 个候选，通过 {len(passed)} 个。")
    config = ctx.read(CONFIG_PATH)
    if config is None:
        chosen, backups = choose(passed, MAX_BACKUPS)
        ctx.sni = chosen
        ui.detail(f"选定 SNI：{chosen.domain}（{chosen.ip}，{chosen.latency_ms} 毫秒），备选 {len(backups)} 个。")
    else:
        current = read_identity(config)["sni"]
        backups = [cand for cand in _fastest_first(passed) if cand.domain != current][:MAX_BACKUPS]
        if not backups:
            ui.warn("没有新的候选通过节点验证，备选 SNI 保持不变。")
            return
        ui.detail(f"保留当前 SNI {current}，备选更新为 {len(backups)} 个。")
    remote.put_text(BACKUPS_PATH, backups_json(backups), "600")
    if ctx.inventory.clear_exhausted(ctx.node.alias):
        ui.detail("备选 SNI 已补充，控制端不再把这个节点记为备选用完。")


def require_direct(ctx: DeployContext) -> None:
    """Go on once this computer reaches the node's network directly; stop when you say so.

    Behind a proxy you are asked to turn it off and check again, so the instance already
    created is kept. When the addresses cannot be read you may confirm a direct connection
    yourself, but not while DNS shows a proxy.
    """
    network = scan_network(ctx.node.ipv4)
    while True:
        seen = _egress(ctx)
        direct = is_direct(seen)
        if direct is True:
            return
        if direct is None:
            question = f"{_route_text(seen, network)}。扫描必须直连进行，不能经过代理，请确认。"
            if ctx.deps.ui.choose(question, UNKNOWN_EGRESS_OPTIONS) != 0:
                raise CnsrError(f"已停止：扫描 {network} 必须直连。")
            return
        question = f"{_route_text(seen, network)}。扫描必须直连，请关闭代理（包括圈X 这类 TUN 模式的代理），然后重新检查。"
        if ctx.deps.ui.choose(question, PROXIED_OPTIONS) != 0:
            raise CnsrError(f"已停止：扫描 {network} 必须直连，请关闭代理后重跑。")


def _egress(ctx: DeployContext) -> Egress:
    """How this computer reaches the node's network: its address as a domestic echo and as the
    node sees it, and whether its DNS answers with a proxy's fake addresses.

    The node is reached by IP over a new connection, the same way the scan goes. ``SSH_CLIENT``
    is read without sudo, which would reset the environment.
    """
    domestic = ctx.deps.direct_address()
    fake_dns = ctx.deps.fake_ip_dns()
    try:
        node = client_address(ctx.remote().run(["printenv", "SSH_CLIENT"]))
    except SshError:
        raise
    except CnsrError:  # printenv fails when the variable is not set
        node = None
    ctx.deps.ui.detail(
        f"直连检查：国内回显服务看到的地址 {domestic or '未知'}，节点看到的来源地址 {node or '未知'}，"
        f"本机 DNS {'返回了' if fake_dns else '没有返回'}代理软件的虚拟地址（{FAKE_IP_NETWORK}）。"
    )
    return Egress(domestic, node, fake_dns)


def _route_text(seen: Egress, network: str) -> str:
    if seen.fake_dns:
        return f"这台电脑在用代理：本机 DNS 把域名解析成了代理软件的虚拟地址（{FAKE_IP_NETWORK}），发往 {network} 的扫描会经过代理"
    if seen.domestic is None:
        return f"无法确认发往节点所在网段 {network} 的流量是否直连（国内回显服务都没有应答）"
    if seen.node is None:
        return f"无法确认发往节点所在网段 {network} 的流量是否直连（节点没有报告你的来源地址）"
    return (
        f"发往节点所在网段 {network} 的流量走了代理：节点看到你从 {seen.node} 连过来，"
        f"而你直连时的出口是 {seen.domestic}"
    )


def _backups_on_node(ctx: DeployContext) -> list[Candidate]:
    text = ctx.read(BACKUPS_PATH)
    if text is None:
        return []
    try:
        return parse_backups(text)
    except CnsrError as exc:
        ctx.deps.ui.warn(f"节点上的备选 SNI 列表无法读取，将重新扫描：{exc}")
        return []


def _fastest_first(cands: list[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda cand: cand.latency_ms)
