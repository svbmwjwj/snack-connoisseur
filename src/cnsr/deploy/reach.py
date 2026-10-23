"""Why this computer cannot reach a server's SSH port, and a new IP for a blocked AWS one.

The control plane tries the port from outside. Open from there means the trouble is between
this computer and the server: a proxy left on, a network that blocks the port, or an address
blocked as a whole. The last shows as a timeout on 443 too (nothing listens there yet, so an
address that answers refuses). A static IP cnsr allocated for a first deployment is then
released and allocated again, up to ``IP_SWAPS`` times.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cnsr.deploy.context import DeployContext
from cnsr.errors import CnsrError

IP_SWAPS = 3
PATH_PORTS = (80, 443)
PATH_TRIES = 2
# Lightsail may still be settling a release: allocating the same name again is retried.
ALLOCATE_TRIES = 3
ALLOCATE_RETRY_SECONDS = 5

# "answering": the SSH port answers from here by now (an sshd that came up meanwhile).
Kind = Literal["unknown", "server", "answering", "proxy", "path", "blocked"]


@dataclass
class Verdict:
    kind: Kind
    host: str
    port: int
    outside_error: str | None = None


def diagnose(ctx: DeployContext, host: str, port: int, *, announce: bool = True) -> Verdict:
    """What stands between this computer and ``host``:``port``, after ssh timed out on it."""
    deps = ctx.deps
    if announce:
        deps.ui.detail(f"这台电脑连不上 {host} 的 {port} 端口，请控制端从外部试一次。")
    try:
        outside = deps.probe(ctx.meta, host, port=port)
    except CnsrError:
        return Verdict("unknown", host, port)
    if not outside.control_reached:
        return Verdict("unknown", host, port)
    if not outside.ok:
        return Verdict("server", host, port, outside.error)
    if deps.local_tcp(host, port) in ("open", "refused"):  # "error" (no route) is no answer
        return Verdict("answering", host, port)
    if deps.fake_ip_dns():
        return Verdict("proxy", host, port)
    tried = {(p, n): deps.local_tcp(host, p) for p in PATH_PORTS for n in range(PATH_TRIES)}
    deps.ui.detail(f"外部能连上；这台电脑连 {host} 其他端口的结果：{tried}。")
    blocked = all(result == "timeout" for result in tried.values())
    return Verdict("blocked" if blocked else "path", host, port)


def can_swap(ctx: DeployContext) -> bool:
    """A static IP cnsr allocated, on a node that was never deployed, with swaps left."""
    node = ctx.node
    return (
        ctx.req.source == "aws"
        and not node.static_ip_reused
        and node.static_ip_name is not None
        and node.line is None
        and ctx.ip_swaps < IP_SWAPS
    )


def swap_ip(ctx: DeployContext) -> None:
    """Release the node's blocked static IP and attach a newly allocated one."""
    node, deps = ctx.node, ctx.deps
    blocked = node.ipv4
    while True:
        ctx.ip_swaps += 1
        deps.ui.info(
            f"静态 IP {node.ipv4} 从这台电脑整个连不上，从外部却能连上，多半已被墙；"
            f"释放它，换一个新 IP（第 {ctx.ip_swaps} 次）。"
        )
        _release(ctx)
        node.ipv4 = _allocate(ctx)
        ctx.save()
        if node.ipv4 != blocked or ctx.ip_swaps >= IP_SWAPS:
            break
        deps.ui.detail("Lightsail 又分配了同一个地址，再换一次。")
    deps.forget_host_key(node.ipv4, node.ssh.port)
    deps.ui.detail(f"新的静态 IP：{node.ipv4}。")


def _release(ctx: DeployContext) -> None:
    node, lightsail = ctx.node, ctx.lightsail
    lightsail.detach_static_ip(node.static_ip_name)  # waits until Lightsail shows it free
    errors = lightsail.release_static_ip(node.static_ip_name)
    if errors:
        raise CnsrError(f"没能释放被墙的静态 IP {node.ipv4}：{'；'.join(errors)}")


def _allocate(ctx: DeployContext) -> str:
    node, lightsail = ctx.node, ctx.lightsail
    for attempt in range(ALLOCATE_TRIES):
        try:
            return lightsail.ensure_static_ip(node.instance_name, node.static_ip_name)
        except CnsrError:
            if attempt == ALLOCATE_TRIES - 1:
                raise
            ctx.deps.sleep(ALLOCATE_RETRY_SECONDS)
    raise AssertionError("unreachable")


def explain(ctx: DeployContext, verdict: Verdict, err: CnsrError) -> CnsrError:
    host, port = verdict.host, verdict.port
    if verdict.kind == "server":
        return CnsrError(
            f"从外部也连不上 {host} 的 {port} 端口（{verdict.outside_error}），问题在服务器一侧："
            "检查服务器是否在运行、SSH 端口是否正确，以及厂商的防火墙。修好后重跑同一条命令。"
        )
    if verdict.kind == "proxy":
        return CnsrError(
            f"从外部能连上 {host} 的 {port} 端口，这台电脑却连不上；这台电脑的 DNS 返回了代理软件的虚拟地址"
            "（198.18.0.0/15），说明代理（例如圈X 的 TUN 模式）开着，SSH 被它转走了。"
            "请先关掉代理，再重跑同一条命令。"
        )
    if verdict.kind == "path":
        return CnsrError(
            f"从外部能连上 {host} 的 {port} 端口，这台电脑却连不上，问题出在这台电脑到服务器的路上："
            "当前网络可能屏蔽或限速了到这台服务器的连接（手机网络常见），也可能是本机代理的规则把它拦了。"
            "换一个网络，或调整代理设置后，重跑同一条命令。"
        )
    if verdict.kind == "blocked":
        return CnsrError(f"{host} 从这台电脑整个连不上（所有端口都超时），从外部却能连上，多半已被墙。{_blocked_advice(ctx)}")
    return err


def _blocked_advice(ctx: DeployContext) -> str:
    node = ctx.node
    if ctx.req.source != "aws":
        return "被墙的 IP 不能用作节点，请联系服务器厂商更换 IP。"
    if node.static_ip_reused:
        return "这是你用 --static-ip 指定的 IP，cnsr 不会释放它：换一个静态 IP，或不带 --static-ip 重跑，让 cnsr 自动申请。"
    if node.line is not None:
        return f"节点已经在用这个 IP；要换 IP，请运行 cnsr rm {node.alias} 后重新部署。"
    return f"已经换了 {IP_SWAPS} 次 IP 都连不上，可以稍后重跑，或换一个区域。"
