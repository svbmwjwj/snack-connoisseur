"""A static IP the user already has, made the node's address (``cnsr up --aws <region> --static-ip``,
or picked in the menu ``choose`` shows).

The IP keeps its name and is marked as reused, so ``cnsr rm`` detaches it and keeps it. One that
is attached to another instance is moved only after the user agrees, and only once the node's
instance runs, so the old instance serves until then; the instance it came from is recorded,
and a first deployment that fails gives the IP back to it. An IP another cnsr node uses is
taken only when it was picked in the menu, as a takeover: the old node's record lets go of
it when it moves, gets it back when the first deployment fails, and the new node keeps it
the way the old one did once it is deployed (``hand_over``).
"""

from __future__ import annotations

from collections.abc import Iterable

from cnsr import features
from cnsr.aws.lightsail import REGION_NAMES, Lightsail
from cnsr.aws.static_ips import StaticIp
from cnsr.deploy.context import AddRequest, DeployContext, Deps
from cnsr.errors import CnsrError
from cnsr.state import Inventory, Node

MOVE_OPTIONS = ["改绑到新节点", "停止"]
TAKEOVER_LABEL = "接替 cnsr 节点 {}"
MENU_PROMPT = "节点地址（AWS 静态 IP）"
STATIC_IP_SUFFIX = "-ip"
UNNAMED = "绑在一台没有名字的实例上"


def asks(deps: Deps, req: AddRequest, existing: Node | None) -> bool:
    """Whether ``choose`` is to be shown: for a node on AWS without ``--static-ip``, with a terminal.

    The node is new, or its record shows that the cleanup after a failed first deployment put
    the user's own static IP back: a rerun then picks again instead of allocating ``<alias>-ip``.
    A node a finished run left anything on is not cleaned up, so ``cnsr rm`` kept that IP.
    """
    if req.source != "aws" or req.static_ip is not None or not deps.ui.interactive:
        return False
    if existing is None:
        return True
    return not features.installed(existing) and existing.static_ip_reused and existing.static_ip_name is None


def choose(
    deps: Deps, lightsail: Lightsail, inventory: Inventory, alias: str, region: str
) -> tuple[str | None, bool, str | None]:
    """Ask which static IP becomes the new node's address, when the region has one to offer.

    The first option is the default: a new IP named ``<alias>-ip``, or that IP when it exists
    and is free (picking it is the consent step 1 would ask for). The region's other IPs follow;
    one another cnsr node uses is offered as a takeover of that node, and one on an instance
    AWS does not name is shown but cannot be picked. Returns the address picked, or None for
    the default, whether the default takes over a free IP, and the cnsr node taken over.
    Nothing is asked when no other IP can be picked; when the default cannot be picked either
    (``<alias>-ip`` is attached elsewhere or used by a cnsr node), the run stops before
    anything is made.
    """
    ips = lightsail.static_ips()
    users = cnsr_users(inventory, region, alias, ((ip.name, ip.attached_to) for ip in ips))
    own = next((ip for ip in ips if ip.name == f"{alias}{STATIC_IP_SUFFIX}"), None)
    blocked = _default_blocked(own, users)
    rows = [(ip, _blocked(ip, users)) for ip in ips if ip is not own]
    # Free IPs, then attached ones, then other cnsr nodes' (a takeover), then those that cannot be picked.
    rows.sort(key=lambda row: (row[1] is not None, row[0].name in users, row[0].attached_to is not None, row[0].name))
    if all(reason is not None for _, reason in rows):
        if blocked is not None:
            raise CnsrError(f"{blocked}。没有创建任何资源。")
        return None, False, None
    if own is None:
        first = "新申请一个静态 IP（cnsr rm 时释放）"
    elif blocked is None:
        first = f"接管空闲的静态 IP {own.name}（cnsr rm 时释放）"
    else:
        first = f"使用同名静态 IP {own.name}"
    options = [first] + [_label(ip, reason, users.get(ip.name)) for ip, reason in rows]
    disabled = {index: reason for index, (_, reason) in enumerate(rows, 1) if reason is not None}
    if blocked is not None:
        disabled[0] = blocked
    index = deps.ui.choose(MENU_PROMPT, options, default=0 if blocked is None else 1, disabled=disabled)
    if index == 0:
        return None, own is not None, None
    picked = rows[index - 1][0]
    return picked.address, False, users.get(picked.name)


def cnsr_users(inventory: Inventory, region: str, alias: str, ips: Iterable[tuple[str, str | None]]) -> dict[str, str]:
    """Which of ``ips`` (name, instance attached to) another cnsr node of the region uses: name to alias.

    A node uses the IP its record names, attached or not (a rerun attaches it again), and any
    IP attached to its instance (a crash can come between attaching an IP and saving its
    name, and ``cnsr rm`` claims such an IP too). Reads no record when ``ips`` is empty.
    """
    ips = list(ips)
    if not ips:
        return {}
    used: dict[str, str] = {}
    for other in inventory.aliases():
        node = inventory.get(other) if other != alias else None
        if node is None or node.source != "aws" or node.region != region:
            continue
        instance = node.instance_name or node.alias
        for name, attached_to in ips:
            if name == node.static_ip_name or (attached_to and attached_to == instance):
                used.setdefault(name, node.alias)
    return used


def _default_blocked(own: StaticIp | None, users: dict[str, str]) -> str | None:
    """Why ``<alias>-ip`` cannot be the node's address; None when it can, or does not exist."""
    if own is None:
        return None
    if own.name in users:
        return f"同名静态 IP {own.name} 是 cnsr 节点 {users[own.name]} 在用的地址，请换一个别名"
    if own.attached_to is not None:
        where = f"实例 {own.attached_to} " if own.attached_to else "一台没有名字的实例"
        return f"同名静态 IP {own.name} 已绑在{where}上，请换一个别名"
    return None


def _blocked(ip: StaticIp, users: dict[str, str]) -> str | None:
    return UNNAMED if ip.attached_to == "" else None


def _label(ip: StaticIp, reason: str | None, user: str | None) -> str:
    if ip.attached_to is None:
        state = "空闲"
    else:
        state = f"绑在实例 {ip.attached_to} 上" if ip.attached_to else "已绑定"
    kept = f"（{TAKEOVER_LABEL.format(user)}）" if user else "（cnsr rm 时保留）" if reason is None else ""
    return f"{ip.name}  {ip.address}  {state}{kept}"


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
    user = cnsr_users(ctx.inventory, ctx.req.region, node.alias, [found]).get(ip_name)
    if user is not None and user == ctx.req.replaces:
        _confirm_takeover(ctx, ip_name, user, instance)
        ctx.replaced = ctx.inventory.get(user)
    elif user is not None:
        raise CnsrError(
            f"静态 IP {address}（{ip_name}）是 cnsr 节点 {user} 在用的地址，已停止，没有创建任何实例。"
            "一个静态 IP 只能给一台节点用：请换一个静态 IP，或不带 --static-ip 重跑。"
        )
    elif attached_to not in (None, instance):
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


def _confirm_takeover(ctx: DeployContext, ip_name: str, user: str, instance: str) -> None:
    address = ctx.req.static_ip
    question = (
        f"静态 IP {address}（{ip_name}）是 cnsr 节点 {user} 的地址。要让新节点 {instance} 接替它吗？"
        f"实例开好后 cnsr 把它改绑过来，{user} 从那时起断线，但它的实例还在计费，部署成功后请自行 cnsr rm {user}；"
        f"部署中途失败时 cnsr 会把它绑回 {user}。"
    )
    try:
        choice = ctx.deps.ui.choose(question, [TAKEOVER_LABEL.format(user), "停止"])
    except CnsrError:
        raise CnsrError(f"静态 IP {address} 是 cnsr 节点 {user} 的地址，cnsr 不会在没有确认时接替它。") from None
    if choice != 0:
        raise CnsrError("已停止，静态 IP 保持原样。没有创建任何实例。")


def _let_go(ctx: DeployContext) -> None:
    """The node taken over no longer names the static IP, so that ``cnsr rm`` of it leaves it alone."""
    old = ctx.inventory.get(ctx.replaced.alias) if ctx.replaced is not None else None
    if old is not None and old.static_ip_name == ctx.node.static_ip_name:
        old.static_ip_name = None
        ctx.inventory.put(old)


def restore(ctx: DeployContext) -> None:
    """Give the node taken over its record back, after the new node's first deployment failed."""
    if ctx.replaced is not None:
        ctx.inventory.put(ctx.replaced)


def hand_over(ctx: DeployContext) -> None:
    """Once the new node is deployed, it keeps the static IP taken over as the old node did:
    released by ``cnsr rm`` when cnsr allocated it, kept when it is the user's own."""
    if ctx.replaced is None:
        return
    ctx.node.static_ip_reused, ctx.node.static_ip_from = ctx.replaced.static_ip_reused, None


def remind(ctx: DeployContext) -> None:
    """After a takeover, the last words of the run: the old node still costs money."""
    if ctx.replaced is None:
        return
    ctx.deps.ui.info("")
    ctx.deps.ui.warn(
        f"静态 IP {ctx.node.static_ip_name} 已从节点 {ctx.replaced.alias} 交给 {ctx.node.alias}；"
        f"{ctx.replaced.alias} 的实例还在计费，确认 {ctx.node.alias} 可用后运行 cnsr rm {ctx.replaced.alias}。"
    )


def take(ctx: DeployContext, instance: str) -> None:
    """Free the reused static IP for ``instance``: detach it from the instance it is on.

    A first deployment moves it off the instance the user agreed to; one that went to any other
    instance meanwhile is left where it is. A node deployed before asks again: the IP went back
    to an instance after that, so the earlier consent no longer holds. A node taken over lets
    go of the IP first.
    """
    node, lightsail = ctx.node, ctx.lightsail
    _let_go(ctx)
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
