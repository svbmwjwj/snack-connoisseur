"""A static IP the user already has, made the node's address (``cnsr up --aws <region> --static-ip``).

The IP keeps its name and is marked as reused, so ``cnsr rm`` detaches it and keeps it. One that
is attached to another instance is moved only after the user agrees, and only once the node's
instance runs, so the old instance serves until then; the instance it came from is recorded,
and a first deployment that fails gives the IP back to it.
"""

from __future__ import annotations

from cnsr.aws.lightsail import REGION_NAMES
from cnsr.deploy.context import DeployContext
from cnsr.errors import CnsrError

MOVE_OPTIONS = ["改绑到新节点", "停止"]


def adopt(ctx: DeployContext, instance: str) -> str:
    """Find the static IP with the requested address and record it as the node's; return its name.

    Asked before the instance exists, so that stopping creates nothing.
    """
    node, address = ctx.node, ctx.req.static_ip
    found = ctx.lightsail.find_static_ip(address)
    if found is None:
        region = REGION_NAMES.get(ctx.req.region, ctx.req.region)
        raise CnsrError(
            f"AWS {region}区域里没有地址为 {address} 的静态 IP。"
            "请检查地址和区域：静态 IP 只能绑到同一区域的实例上。"
        )
    ip_name, attached_to = found
    if attached_to not in (None, instance):
        _confirm_move(ctx, ip_name, attached_to, instance, "没有创建任何实例。")
    node.static_ip_name, node.static_ip_reused, node.static_ip_from = ip_name, True, attached_to
    ctx.deps.ui.detail(f"节点地址用你已有的静态 IP {ip_name}（{address}）。")
    return ip_name


def _confirm_move(ctx: DeployContext, ip_name: str, attached_to: str, instance: str, stopped: str = "") -> None:
    address = ctx.req.static_ip or ctx.node.ipv4
    # Only a first deployment is cleaned up, and only then does the IP go back by itself.
    back = f"部署中途失败时 cnsr 会把它绑回 {attached_to}。" if ctx.node.line is None else ""
    question = (
        f"静态 IP {address}（{ip_name}）现在绑在实例 {attached_to} 上。要改绑到节点 {instance} 吗？"
        f"节点的实例开好后 cnsr 会把它从 {attached_to} 解绑再绑过来，{attached_to} 从那时起改用临时公网 IP，"
        f"经这个 IP 访问它的服务会断开。{back}"
    )
    try:
        choice = ctx.deps.ui.choose(question, MOVE_OPTIONS)
    except CnsrError:
        raise CnsrError(
            f"静态 IP {address} 绑在实例 {attached_to} 上，cnsr 不会在没有确认时改绑它。"
            "请在终端里重跑同一条命令并确认。"
        ) from None
    if choice != 0:
        raise CnsrError(f"已停止，静态 IP 保持原样。{stopped}")


def take(ctx: DeployContext, instance: str) -> None:
    """Free the reused static IP for ``instance``: detach it from the instance it is on.

    A first deployment moves it off the instance the user agreed to; one that went to any other
    instance meanwhile is left where it is. A node deployed before asks again: the IP went back
    to an instance after that, so the earlier consent no longer holds.
    """
    node, lightsail = ctx.node, ctx.lightsail
    _, attached_to = lightsail.static_ip_attachment(node.static_ip_name)
    if attached_to in (None, instance):
        return
    if node.line is not None:
        _confirm_move(ctx, node.static_ip_name, attached_to, instance)
        node.static_ip_from = attached_to
    elif attached_to != node.static_ip_from:
        raise CnsrError(
            f"静态 IP {node.static_ip_name} 现在绑在实例 {attached_to} 上，不是你同意改绑的 "
            f"{node.static_ip_from or '（未绑定）'}，已停止，没有动它。"
        )
    ctx.deps.ui.detail(f"把静态 IP {node.static_ip_name} 从实例 {attached_to} 解绑。")
    lightsail.detach_static_ip(node.static_ip_name)


def give_back(ctx: DeployContext, ip_name: str, instance: str) -> None:
    """Attach the reused static IP to the instance it came from again, after a failed first deployment.

    An IP still on that instance (the run failed before moving it) needs nothing, and no message.
    """
    ui = ctx.deps.ui
    try:
        if ctx.lightsail.static_ip_attachment(ip_name)[1] == instance:
            return
        ctx.lightsail.ensure_static_ip(instance, ip_name)
    except CnsrError as exc:
        ui.warn(f"没能把静态 IP {ip_name} 绑回实例 {instance}：{exc} 请到 Lightsail 控制台手动绑回。")
        return
    ui.info(f"已把静态 IP 绑回实例 {instance}。")
