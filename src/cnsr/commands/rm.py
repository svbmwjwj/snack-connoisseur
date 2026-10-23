"""``cnsr rm``: delete a node's cloud resources and records, in an order that a rerun can finish.

The steps run in a fixed order: the AWS instance and static IP, the DNS records, the SSH
alias, and last the node's entry in the control plane (which also holds its client config
line and its token), together with its SNI rotation state. A step that fails does not stop the ones after it, but the entry stays,
so that running ``rm`` again retries whatever is left. The entry is updated with what was
deleted, so a rerun does not repeat it.

Nothing is ever done on the server itself, and the public key file and the agent's keys stay.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable

from cnsr import localconf
from cnsr.aws.lightsail import Lightsail
from cnsr.cloudflare.api import CloudflareClient
from cnsr.cloudflare.control import find_control_plane
from cnsr.commands.token_hint import saved_token_hint
from cnsr.deploy.context import Deps, default_deps, known_lightsail
from cnsr.deploy.dns_records import delete_node_records
from cnsr.deploy.steps_host import STATIC_IP_SUFFIX
from cnsr.errors import CnsrError
from cnsr.ssh.config import HostBlock, read_block, remove_block
from cnsr.state import Inventory, Node

RM_TITLES = {"aws": "删除 AWS 实例和静态 IP", "dns": "删除 DNS 记录", "alias": "删除 SSH 别名", "record": "删除控制端登记"}
# A static IP you had before the node (cnsr add --static-ip) is detached and kept, not deleted.
KEPT_IP_TITLE = "删除 AWS 实例（静态 IP 保留）"


def run(ns: Namespace, deps: Deps | None = None) -> int:
    """Delete the node ``ns.alias``; 0 when everything is gone, 1 after an error or a leftover.

    The error is printed here. Ctrl-C goes up to ``cnsr.cli``.
    """
    deps = deps or default_deps()
    try:
        with saved_token_hint(deps.conf_path):
            return _remove(ns.alias, deps)
    except CnsrError as exc:
        deps.ui.error(str(exc))
        return 1


def _remove(alias: str, deps: Deps) -> int:
    ui = deps.ui
    cf, zone_id, inventory, node = _find_node(alias, deps)
    _show_plan(node, deps)
    ui.confirm_text("输入别名确认删除", alias)

    keys = rm_stages(node)
    ui.begin(f"cnsr rm {alias}", [stage_title(key, node) for key in keys])

    def on_stage(key: str, previous_left: bool) -> None:
        ui.stage(keys.index(key) + 1, "failed" if previous_left else "done")

    try:
        left = remove_node(cf, zone_id, inventory, node, deps, on_stage=on_stage)
    except KeyboardInterrupt:
        ui.end("interrupted")
        raise
    except BaseException:
        ui.end("failed")
        raise
    ui.end("failed" if left else "done")  # the record stays while anything is left
    if not left:
        ui.info(f"节点 {alias} 已删除。")
        return 0
    ui.warn(f"节点 {alias} 没有删干净，它的记录已保留。以下几项需要处理：")
    for item in left:
        ui.info(f"  — {item}")
    ui.info(f"处理好之后，重新运行 cnsr rm {alias} 补删。")
    return 1


def stage_title(key: str, node: Node) -> str:
    return KEPT_IP_TITLE if key == "aws" and node.static_ip_reused else RM_TITLES[key]


def rm_stages(node: Node) -> list[str]:
    """The parts ``remove_node`` goes through for ``node``, as keys of ``RM_TITLES``, in order."""
    return (["aws"] if node.source == "aws" else []) + ["dns", "alias", "record"]


def remove_node(
    cf: CloudflareClient,
    zone_id: str,
    inventory: Inventory,
    node: Node,
    deps: Deps,
    *,
    on_stage: Callable[[str, bool], None] | None = None,
    ip_goes_back: bool = False,
) -> list[str]:
    """Delete everything the node's record names, the record last; return what is left.

    ``ip_goes_back``: a kept static IP is attached to its old instance right after, so the note
    that a free static IP is charged by the hour goes to the log only.

    Something left keeps the record, updated with what was deleted. ``cnsr add`` uses this
    too, to clean up after a first AWS deployment that failed. ``on_stage(key, previous_left)``
    is called before each part of ``rm_stages(node)`` starts, whether or not it has anything
    to do; ``previous_left`` says whether the part before it left something.
    """
    _withdraw_line(inventory, node, deps)
    recorded = node.to_json()
    left: list[str] = []
    seen = 0

    def enter(key: str) -> None:
        nonlocal seen
        if on_stage is not None:
            on_stage(key, len(left) > seen)
        seen = len(left)

    try:
        if node.source == "aws":
            enter("aws")
            left += _remove_aws(node, deps, ip_goes_back)
        enter("dns")
        left += _remove_dns(cf, zone_id, node, deps)
        enter("alias")
        left += _remove_alias(node, deps)
        enter("record")
    except BaseException:  # Ctrl-C or a bug: what was deleted so far must not be repeated blindly
        _save_progress(inventory, node, recorded, deps)
        raise
    if not left:
        deps.ui.detail("从控制端删除节点登记和它的换 SNI 状态。")
        try:
            inventory.delete_state(node.alias)  # first: while the record stays, a rerun retries it
            inventory.delete(node.alias)
        except CnsrError as exc:
            left.append(f"从控制端清单里删除 {node.alias} 失败：{exc}")
    if left:
        _save_progress(inventory, node, recorded, deps)
    return left


def _find_node(alias: str, deps: Deps) -> tuple[CloudflareClient, str, Inventory, Node]:
    """The node's record and what is needed to delete the rest; creates nothing in Cloudflare."""
    values = localconf.load(deps.conf_path)
    token, zone = values.get("CLOUDFLARE_API_TOKEN"), values.get("CLOUDFLARE_ZONE")
    if not (token and zone):
        raise CnsrError(f"没有名为 {alias} 的节点（本机还没有 Cloudflare 设置）。")
    cf = deps.make_cf(token)
    found = find_control_plane(cf, zone)
    if found is None:
        raise CnsrError(f"没有名为 {alias} 的节点（这个 Cloudflare 账号里还没有控制端）。")
    kv, _, zone_id = found
    inventory = Inventory(kv)
    node = inventory.get(alias)
    if node is None:
        raise CnsrError(f"没有名为 {alias} 的节点。")
    return cf, zone_id, inventory, node


def _show_plan(node: Node, deps: Deps) -> None:
    """List what will be deleted and what stays."""
    ui = deps.ui
    ui.info(f"将删除节点 {node.alias}：")
    keep = ["~/.ssh/pub 里的公钥文件，SSH agent 里的钥匙"]
    made = deps.key_dir / node.ssh.key_name if node.ssh else None
    if made is not None and made.exists():
        keep.append(f"{made}（cnsr 生成的私钥；别的服务器还在用就请保管好，不用了可以自行删除它和 {made.name}.pub）")
    if node.source == "aws":
        if node.instance_name:
            ui.info(f"  — AWS Lightsail 实例 {node.instance_name}（区域 {node.region}）")
        else:
            ui.info(f"  — AWS Lightsail 实例 {node.alias}（区域 {node.region}；记录里没有它，只有它带着 cnsr 给这个节点的标签才会删除）")
        if node.static_ip_name and node.static_ip_reused:
            ui.info(f"  — 静态 IP {node.static_ip_name}（你原有的，只解绑、不释放）")
        elif node.static_ip_name:
            ui.info(f"  — 静态 IP {node.static_ip_name}")
        else:
            guess, instance = f"{node.alias}{STATIC_IP_SUFFIX}", node.instance_name or node.alias
            ui.info(f"  — 静态 IP {guess}（记录里没有它，只有它绑在实例 {instance} 上才会释放）")
        keep.append("AWS 上的 key pair（同区域的其他实例可能还在用）")
    else:
        keep.insert(0, f"服务器 {node.ipv4} 本身不动（上面的 Xray 仍在运行，需要时请自行处理）")
    if node.dns_record_ids:
        ui.info(f"  — DNS 记录 {'、'.join(node.dns_record_ids)}（{node.domain}）")
    block = read_block(deps.ssh_config, node.alias)
    if block is not None and _is_ours(block, node):
        ui.info(f"  — SSH 别名 {node.alias}（~/.ssh/config 里指向 {block.hostname} 的那一段）")
    elif block is not None and block.hostname:
        keep.append(f"SSH 别名 {node.alias} 指向 {block.hostname}，不是这台节点的地址")
    ui.info("  — 控制端里的节点登记（圈X 配置行、节点 token 和换 SNI 的状态）")
    ui.info("保留不动：")
    for item in keep:
        ui.info(f"  — {item}")


def _hostnames(node: Node) -> set[str]:
    """The HostNames that make an SSH alias this node's: its IPv4 and its domain."""
    return {host for host in (node.ipv4, node.domain) if host}


def _is_ours(block: HostBlock, node: Node) -> bool:
    """Whether the alias block points at this node, by its IP or by its domain."""
    return block.hostname in _hostnames(node)


def _remove_aws(node: Node, deps: Deps, ip_goes_back: bool = False) -> list[str]:
    """Release the static IP and delete the instance; return what could not be done.

    Only what the record names is touched, and only after a look at AWS shows that it is
    cnsr's. The instance must carry cnsr's tag for this alias, else it and its static IP are
    left alone. Without a recorded instance, one named after the alias that carries the tag
    is cnsr's too (a crash can come between creating it and saving its name). Each name
    leaves the record once its resource is gone.
    """
    if node.source != "aws":
        return []
    recorded = node.instance_name is not None or node.static_ip_name is not None
    try:
        lightsail = _lightsail(node, deps)
        tagged = _find_instance(node, lightsail)
    except CnsrError as exc:
        if recorded:
            return [str(exc)]
        deps.ui.warn(
            f"没能检查 AWS 上有没有 cnsr 为 {node.alias} 创建、但没记下的实例：{exc}"
            "如果有，请到 Lightsail 控制台自行删除。"
        )
        return []
    name = node.instance_name
    if tagged is False:
        return [f"实例 {name} 不是 cnsr 创建的，未删除"]
    if name is None and node.static_ip_name is None:
        return []
    deps.ui.detail("删除 AWS 上的静态 IP 和实例。")
    left = _release_static_ip(node, lightsail, deps, ip_goes_back)
    if name is not None:
        errors = lightsail.delete_instance(name) if tagged else []  # None: the instance is already gone
        if not errors:
            node.instance_name = None
        left += errors
    return left


def _find_instance(node: Node, lightsail: Lightsail) -> bool | None:
    """Whether the node's instance carries cnsr's tag; None when there is no instance.

    With no recorded instance, an instance named after the alias that carries the tag is the
    node's: its name goes into the record. One without the tag is not the node's at all.
    """
    if node.instance_name is not None:
        return lightsail.instance_tagged(node.instance_name, node.alias)
    if lightsail.instance_tagged(node.alias, node.alias):
        node.instance_name = node.alias
        return True
    return None


def _release_static_ip(node: Node, lightsail: Lightsail, deps: Deps, ip_goes_back: bool = False) -> list[str]:
    """Release the node's static IP when it is cnsr's.

    A recorded IP is cnsr's while it is free or attached to the recorded instance; one attached
    to another instance is left alone. Without a record, ``<alias>-ip`` is looked at only when
    an instance is recorded (a crash can come between attaching the IP and saving its name):
    attached to that instance it is cnsr's; a free one may be a leftover, which is for the user
    to check; one attached elsewhere is not ours.
    """
    recorded = node.static_ip_name
    if recorded is None and node.instance_name is None:
        return []
    if recorded is not None and node.static_ip_reused:
        return _keep_static_ip(node, lightsail, deps, ip_goes_back)
    ip_name = recorded or f"{node.alias}{STATIC_IP_SUFFIX}"
    try:
        exists, attached_to = lightsail.static_ip_attachment(ip_name)
        if not exists:
            node.static_ip_name = None
            return []
        if attached_to not in (None, node.instance_name):
            return [f"静态 IP {ip_name} 绑在实例 {attached_to} 上，未释放"] if recorded else []
        if recorded is None and attached_to is None:
            deps.ui.warn(
                f"静态 IP {ip_name} 没有绑在任何实例上，可能是 cnsr 留下的；"
                "请到 Lightsail 控制台确认，是的话自行释放。"
            )
            return []
        node.static_ip_name = ip_name  # proven cnsr's: a release that fails is retried by the next rm
        errors = lightsail.release_static_ip(ip_name)
    except CnsrError as exc:
        return [str(exc)]
    if not errors:
        node.static_ip_name = None
    return errors


def _keep_static_ip(node: Node, lightsail: Lightsail, deps: Deps, ip_goes_back: bool = False) -> list[str]:
    """Detach the static IP the user had before the node from the node's instance, and keep it."""
    ip_name = node.static_ip_name
    try:
        exists, attached_to = lightsail.static_ip_attachment(ip_name)
        if exists and attached_to is not None and attached_to == node.instance_name:
            lightsail.detach_static_ip(ip_name)
    except CnsrError as exc:
        return [str(exc)]
    if exists and attached_to not in (None, node.instance_name):
        deps.ui.info(f"静态 IP {ip_name} 是你原有的，现在绑在实例 {attached_to} 上，保持不动。")
    elif exists:
        say = deps.ui.detail if ip_goes_back else deps.ui.info
        say(
            f"静态 IP {ip_name}（{node.ipv4}）是你原有的，已解绑并保留。"
            "Lightsail 的静态 IP 不绑实例时按小时收费，不再用的话请到 Lightsail 控制台释放。"
        )
    node.static_ip_name = None
    return []


def _lightsail(node: Node, deps: Deps) -> Lightsail:
    if node.region is None:
        raise CnsrError("节点记录里没有 AWS 区域，无法删除 AWS 上的资源。")
    lightsail = known_lightsail(deps, node.region)
    if lightsail is None:
        raise CnsrError(
            "没有找到 AWS 凭据（环境变量、~/.aws 和本机保存的 access key 里都没有），无法删除 AWS 上的资源。"
            "请配置好 AWS 凭据后重新运行。"
        )
    return lightsail


def _remove_dns(cf: CloudflareClient, zone_id: str, node: Node, deps: Deps) -> list[str]:
    """Delete the node's DNS records; the ids of the ones deleted leave the record."""
    if not node.dns_record_ids:
        return []
    deps.ui.detail(f"删除 DNS 记录 {'、'.join(node.dns_record_ids)}（{node.domain}）。")
    return delete_node_records(cf, zone_id, node)


def _remove_alias(node: Node, deps: Deps) -> list[str]:
    """Delete the SSH alias when it points at this node; one that is absent or points elsewhere stays.

    Another ``Host <alias>`` block in the file, one pointing elsewhere, is never touched.
    """
    try:
        block = read_block(deps.ssh_config, node.alias)
        if block is not None and _is_ours(block, node) and remove_block(deps.ssh_config, node.alias, _hostnames(node)):
            deps.ui.detail(f"已删除 SSH 别名 {node.alias}。")
    except CnsrError as exc:
        return [f"SSH 别名 {node.alias}：{exc}"]
    return []


def _withdraw_line(inventory: Inventory, node: Node, deps: Deps) -> None:
    """Take the node's line out of the subscription before anything is deleted, so that a
    removal left half done does not keep handing devices a node that is going away. A failed
    write is only a warning: what costs money is deleted all the same."""
    if node.line is None:
        return
    node.line = None
    try:
        inventory.put(node)
    except Exception as exc:
        deps.ui.warn(f"没能先把 {node.alias} 从订阅里撤下，删完记录后它才会消失：{exc}")


def _save_progress(inventory: Inventory, node: Node, recorded: str, deps: Deps) -> None:
    """Store the node again when the steps changed it, so that a rerun only retries what is left.

    A failed write is only a warning: every step is safe to repeat.
    """
    if node.to_json() == recorded:
        return
    try:
        inventory.put(node)
    except Exception as exc:
        deps.ui.warn(f"没能把删除进度写入控制端，重跑 rm 时会再检查一遍：{exc}")
