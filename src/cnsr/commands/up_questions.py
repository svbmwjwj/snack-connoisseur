"""The questions of ``cnsr up``, in the spec's order, each a step ``Ui.ask_in_order`` can go back to.

Alias, source, the region or the server's address and login (a new Hetzner server: the token,
the plan, the location and the type, as Hetzner's console asks), the features to deploy, the
cloud's credentials, the size of a new AWS node and its static IP, then the SSH agent and key. Whatever the arguments, the control plane's inventory or ``~/.ssh/config``
already say is not asked again.
"""

from __future__ import annotations

import ipaddress
import re
from argparse import Namespace
from types import SimpleNamespace

from cnsr import features, runlog
from cnsr.aws.lightsail import LOGIN_USER
from cnsr.commands import aws_setup, hetzner_setup, size_menu
from cnsr.commands.add_keys import choose_login, pub_file
from cnsr.deploy import static_ip
from cnsr.deploy.context import AddRequest, Deps
from cnsr.aws import bundles
from cnsr.errors import CnsrError
from cnsr.naming import validate_alias
from cnsr.ssh.config import HostBlock, read_block
from cnsr.state import Inventory, Node, is_cloud

SOURCES = ("vps", "aws", "hetzner")
SOURCE_LABELS = ["已有服务器（输入 IP）", "在 AWS Lightsail 新开一台", "在 Hetzner Cloud 新开一台"]
# What each cloud is called when a rerun's arguments do not match its record.
VENDORS = {"aws": "AWS", "hetzner": "Hetzner"}
# What ``--to`` names each feature: the feature to deploy up to, with every one before it.
TO_FEATURES = {"base": features.HARDEN, "xray": features.XRAY}


def answers(ns: Namespace, deps: Deps, inventory: Inventory) -> SimpleNamespace:
    """Ask what the arguments and records leave open; the answers as attributes.

    ``alias``, ``existing`` (its record, None for a new server), ``block`` (its SSH alias),
    ``req``, ``installed``, ``chosen`` (the features), ``lightsail``, ``hetzner``, ``offer``,
    ``agent``, ``key`` and ``key_name``.
    """
    ui = deps.ui
    a = SimpleNamespace()  # the answers, filled in by the steps below

    def ask_alias() -> None:
        a.alias = _alias(ns, deps)
        runlog.alias_known(a.alias)
        a.existing, a.block = inventory.get(a.alias), read_block(deps.ssh_config, a.alias)
        a.source = a.existing.source if a.existing is not None else None

    def ask_source() -> None:
        a.source = a.source if a.existing is not None else _source(ns, deps)

    def ask_where() -> None:
        new = _new_request(ns, deps, a.alias, a.block, a.source) if a.existing is None else None
        a.req, a.installed = new or _rerun_request(ns, a.existing), features.installed(a.existing)

    def ask_features() -> None:
        a.chosen = _features(ns, deps, a.installed)

    def ask_cloud() -> None:
        a.lightsail = aws_setup.lightsail_for(deps, a.req.region) if a.req.source == "aws" else None
        a.hetzner = hetzner_setup.hetzner_for(deps, a.req.region) if a.req.source == "hetzner" else None

    def ask_size() -> None:
        a.offer = None
        if a.existing is None and (a.lightsail or a.hetzner) is not None:  # a rerun keeps its size, unasked
            size = a.req.size if a.req.source == "hetzner" else ns.size  # a Hetzner type is picked with its plan
            a.offer = size_menu.choose(ui, a.lightsail or a.hetzner, a.req.source, a.req.region, size)
            a.req.size = a.offer.size

    def ask_static_ip() -> None:
        a.req.static_ip, a.req.take_free_ip, a.req.replaces = ns.static_ip, False, None
        if a.lightsail is not None and static_ip.asks(deps, a.req, a.existing):
            a.req.static_ip, a.req.take_free_ip, a.req.replaces = static_ip.choose(
                deps, a.lightsail, inventory, a.alias, a.req.region
            )

    def ask_key() -> None:
        a.agent, a.key, a.key_name = choose_login(deps, a.alias, _key_name_hint(a.alias, a.existing, a.block))
        pub_file(deps, a.key, a.key_name)

    deps.ui.ask_in_order([ask_alias, ask_source, ask_where, ask_features, ask_cloud, ask_size, ask_static_ip, ask_key])
    return a


def _features(ns: Namespace, deps: Deps, installed: list[str]) -> list[str]:
    """The features to deploy: ticked in a menu, where those already deployed cannot be unticked.

    Nothing is asked when ``--to`` names the last feature to deploy, when every feature is
    deployed already, nor without a terminal, where every feature is deployed.
    """
    if getattr(ns, "features", None) is not None:
        chosen = list(ns.features)
    elif getattr(ns, "to", None) is not None:
        chosen = list(features.ALL[: features.ALL.index(TO_FEATURES[ns.to]) + 1])
    elif set(installed) == set(features.ALL) or not deps.ui.interactive:
        chosen = list(features.ALL)
    else:
        names = list(features.ALL)
        ticks = deps.ui.choose_many(
            "要部署哪些功能？",
            [features.LABELS[name] for name in names],
            checked=set(range(len(names))),
            locked={names.index(name) for name in installed},
            validate=lambda picked: features.problem([names[index] for index in picked]),
            requires={names.index(name): {names.index(features.BASE)} for name in features.EXTENSIONS},
        )
        chosen = [names[index] for index in ticks]
    problem = features.problem(chosen)
    if problem is not None:
        raise CnsrError(problem)
    return [name for name in features.ALL if name in chosen or name in installed]


def _alias(ns: Namespace, deps: Deps) -> str:
    if ns.alias is None:
        return deps.ui.ask("服务器别名（例如 jp-1）", validate=validate_alias)
    error = validate_alias(ns.alias)
    if error is not None:
        raise CnsrError(error)
    return ns.alias


def _rerun_request(ns: Namespace, node: Node) -> AddRequest:
    """The request for a node the inventory knows; the arguments may only repeat it."""
    # An AWS node recorded before sizes were has the default bundle.
    size = node.size or (bundles.DEFAULT_SIZE if node.source == "aws" else None)
    if (
        (ns.aws and (node.source != "aws" or ns.aws != node.region))
        or (ns.hetzner and (node.source != "hetzner" or ns.hetzner != node.region))
        or (ns.size and ns.size != size)
        or (ns.ip and (node.source != "vps" or ns.ip != node.ipv4))
        or (ns.static_ip and (not node.static_ip_reused or ns.static_ip != node.ipv4))
    ):
        where = f"已有服务器 {node.ipv4}"
        if is_cloud(node.source):
            where = f"{VENDORS[node.source]} {node.region} {size}"
        raise CnsrError(
            f"服务器 {node.alias} 已存在（{where}），和这次的参数不一致。"
            f"要部署到别处，请换一个别名，或先运行 cnsr rm {node.alias}。"
        )
    default_user = LOGIN_USER if node.source == "aws" else "root"
    user, port = (node.ssh.user, node.ssh.port) if node.ssh else (default_user, 22)
    ip = node.ipv4 if node.source == "vps" else None
    return AddRequest(node.alias, node.source, node.region, ip, user, port, static_ip=ns.static_ip, size=size)


def _source(ns: Namespace, deps: Deps) -> str:
    """Where a new server comes from: the arguments say, else the menu."""
    source = "aws" if ns.aws else "hetzner" if ns.hetzner else "vps" if ns.ip else None
    return source or SOURCES[deps.ui.choose("服务器来源", SOURCE_LABELS)]


def _new_request(ns: Namespace, deps: Deps, alias: str, block: HostBlock | None, source: str) -> AddRequest:
    """The request for a node the inventory does not know, from ``source``.

    An alias in ``~/.ssh/config`` that already names a host is used only when it names the
    server to take over; otherwise it belongs to another machine and the run stops here.
    """
    ui = deps.ui
    named = block.hostname if block is not None else ""
    if source == "hetzner":
        if named:
            raise _foreign_alias(deps, alias, named, "而这次要在 Hetzner Cloud 新开一台服务器")
        if ns.size is None and ui.interactive:  # the plan, location and type, as Hetzner's console asks
            region, offer = hetzner_setup.choose_server(deps, ns.hetzner)
            return AddRequest(alias, "hetzner", region, None, "root", 22, size=offer.name)
        region = ns.hetzner or hetzner_setup.choose_region(deps)
        return AddRequest(alias, "hetzner", region, None, "root", 22, size=ns.size)
    if source == "aws":
        if named:
            raise _foreign_alias(deps, alias, named, "而这次要在 AWS Lightsail 新开一台实例")
        region = ns.aws or aws_setup.choose_region(deps)
        return AddRequest(alias, "aws", region, None, LOGIN_USER, 22, static_ip=ns.static_ip, size=ns.size)
    if ns.ip is not None and (error := ipv4_error(ns.ip)) is not None:
        raise CnsrError(error)
    ip = ns.ip
    if ip is None and named and ipv4_error(named) is None:
        ip = named  # the alias already says which server
    if named and named != ip:
        reason = f"不是这次要接管的服务器 {ip}" if ip else "不是 IPv4 地址，无法确认它就是这次要接管的服务器"
        raise _foreign_alias(deps, alias, named, reason)
    if block is not None and _logs_in_to(block, ip):
        return AddRequest(alias, "vps", None, block.hostname, block.user, block.port)
    ip = ip or ui.ask("服务器的 IPv4 地址", validate=ipv4_error)
    user = ui.ask("SSH 登录用户", default="root", validate=_user_error)
    port = int(ui.ask("SSH 端口", default="22", validate=_port_error))
    return AddRequest(alias, "vps", None, ip, user, port)


def _foreign_alias(deps: Deps, alias: str, hostname: str, reason: str) -> CnsrError:
    return CnsrError(
        f"{deps.ssh_config} 里已有 Host {alias}，指向 {hostname}，{reason}。"
        f"为免改掉另一台机器的别名，已停止：请换一个别名，或先从 {deps.ssh_config} 删除 Host {alias} 那一段再重跑。"
    )


def _logs_in_to(block: HostBlock, ip: str | None) -> bool:
    """Whether the alias's own SSH config block says how to log in to ``ip`` (to its IPv4 when None)."""
    if not block.user or ipv4_error(block.hostname) is not None:
        return False
    return ip is None or block.hostname == ip


def _key_name_hint(alias: str, existing: Node | None, block: HostBlock | None) -> str:
    """The key name to look for: the inventory's, else the alias's public key file, else the alias."""
    if existing is not None and existing.ssh is not None:
        return existing.ssh.key_name
    if block is not None and block.identity_file.endswith(".pub"):
        return re.split(r"[\\/]", block.identity_file)[-1].removesuffix(".pub") or alias
    return alias


def ipv4_error(value: str) -> str | None:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return f"{value} 不是 IPv4 地址，请输入服务器的公网 IPv4 地址。"
    return None


def _user_error(value: str) -> str | None:
    return None if re.fullmatch(r"[a-z_][a-z0-9_.-]*", value) else "用户名只能包含小写字母、数字、下划线、点和连字符。"


def _port_error(value: str) -> str | None:
    return None if value.isdecimal() and 1 <= int(value) <= 65535 else "端口必须是 1 到 65535 之间的数字。"


