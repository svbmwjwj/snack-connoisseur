"""``cnsr add``: gather what the deployment needs, run the pipeline, print the client config.

Questions follow the spec's order: Cloudflare (first run only), alias, source, AWS region and
credentials or the server's address and login, then the SSH agent and key. Whatever the
arguments, the control plane's inventory or ``~/.ssh/config`` already say is not asked again.
"""

from __future__ import annotations

import ipaddress
import re
from argparse import Namespace

from cnsr import localconf, runlog
from cnsr.aws.lightsail import LOGIN_USER, REGIONS, Lightsail
from cnsr.cloudflare.api import CloudflareClient
from cnsr.cloudflare.control import check_token, ensure_control_plane
from cnsr.deploy import static_ip
from cnsr.commands.add_keys import choose_login, pub_file
from cnsr.commands.rm import remove_node
from cnsr.commands.token_hint import saved_token_hint
from cnsr.deploy.context import AddRequest, DeployContext, Deps, default_deps, known_lightsail
from cnsr.deploy.steps import run_pipeline
from cnsr.errors import CnsrError
from cnsr.naming import validate_alias
from cnsr.qx import client_lines, v2rayn_url
from cnsr.ssh.config import HostBlock, read_block
from cnsr.state import Inventory, Node, SshInfo

REGION_LABELS = {"jp": "jp（东京）", "sg": "sg（新加坡）", "us": "us（俄勒冈）"}
SOURCES = ("vps", "aws")
SOURCE_LABELS = ["已有服务器（输入 IP）", "在 AWS Lightsail 新开一台"]
TAG_REGEX_EXAMPLE = "^(self-built|cnsr)$"
_ZONE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def run(ns: Namespace, deps: Deps | None = None) -> int:
    """Deploy the node ``ns.alias``, or bring it up to date; 0 on success, 1 after an error.

    The error is printed here. Ctrl-C and unexpected errors go up to ``cnsr.cli``, which reports
    them. In every case, a first AWS deployment that did not finish first deletes what it created.
    """
    deps = deps or default_deps()
    ctx: DeployContext | None = None
    try:
        with saved_token_hint(deps.conf_path):
            ctx = prepare(ns, deps)
            run_pipeline(ctx)
            ctx.inventory.keep_indexed(ctx.node.alias)
    except CnsrError as exc:
        deps.ui.error(str(exc))
        _clean_up(ctx)
        return 1
    except (KeyboardInterrupt, Exception):  # Ctrl-C or an unexpected error: ``cnsr.cli`` reports it
        _clean_up(ctx)
        raise
    _print_summary(ctx)
    return 0


def _clean_up(ctx: DeployContext | None) -> None:
    """Delete what a first AWS deployment that stopped part way created, so nothing keeps costing.

    A node that was deployed before is in use and stays as it is, and so does a vendor server,
    which is not cnsr's to delete. After the cleanup the next run starts from scratch.
    """
    if ctx is None or ctx.req.source != "aws" or ctx.deployed:
        return
    ui, node = ctx.deps.ui, ctx.node
    alias = node.alias
    # rm keeps a reused static IP: it goes back to the instance it was moved from
    moved = (node.static_ip_name, node.static_ip_from) if node.static_ip_reused else (None, None)
    cleaning = "部署没有完成，删除这次在 AWS 和 Cloudflare 上建的实例、静态 IP、DNS 记录和节点登记"
    ui.busy(cleaning)
    ui.detail(cleaning)
    try:
        left = remove_node(ctx.cf, ctx.zone_id, ctx.inventory, ctx.node, ctx.deps, ip_goes_back=bool(moved[1]))
    except CnsrError as exc:
        left = [str(exc)]
    except BaseException:
        ui.idle()
        ui.warn(f"清理被打断。请运行 cnsr rm {alias} 删除剩下的部分，免得继续计费。")
        if moved[0] and moved[1]:
            ui.warn(f"静态 IP {moved[0]} 原来绑在实例 {moved[1]} 上，如果它现在没有绑定，请到 Lightsail 控制台把它绑回去。")
        raise
    ui.idle()
    if moved[0] and moved[1]:
        static_ip.give_back(ctx, *moved)
    if not left:
        ui.info("已清理干净，不会继续计费。修好问题后重跑同一条命令会重新开一台。")
        return
    ui.warn(f"以下几项没有删掉，可能还在计费，请运行 cnsr rm {alias} 补删：")
    for item in left:
        ui.info(f"  — {item}")


def prepare(ns: Namespace, deps: Deps) -> DeployContext:
    """Ask what is missing and build the context the pipeline runs with."""
    ui = deps.ui
    _check_static_ip(ns)
    cf, zone = _cloudflare(deps)
    ui.busy("检查 Cloudflare 控制端")
    ui.detail("检查 Cloudflare 控制端：没有就部署，已有就更新到当前版本。")
    try:
        kv, meta, zone_id = ensure_control_plane(cf, zone, deps.rng)
        deps.check_control(meta)  # before anything is created: a blocked control plane is useless
    finally:
        ui.idle()
    inventory = Inventory(kv)
    alias = _alias(ns, deps)
    runlog.alias_known(alias)
    existing = inventory.get(alias)
    block = read_block(deps.ssh_config, alias)
    req = _rerun_request(ns, existing) if existing is not None else _new_request(ns, deps, alias, block)
    lightsail = _lightsail(deps, req.region) if req.source == "aws" else None
    agent, key, key_name = choose_login(deps, alias, _key_name_hint(alias, existing, block))
    pub_file(deps, key, key_name)
    if existing is None:
        # A device's report right after an earlier rm may have left a rotation state behind,
        # which would send the new node straight into a rotation or a pause.
        inventory.delete_state(alias)
    saved = existing.to_json() if existing is not None else None
    node = existing or Node(alias=alias, source=req.source, ipv4="", region=req.region)
    node.ssh = SshInfo(req.user, req.port, key_name)
    return DeployContext(
        req, deps, node, inventory, cf, zone_id, zone, meta, key, agent,
        lightsail=lightsail, saved=saved, deployed=existing is not None and existing.line is not None,
    )  # fmt: skip


def _cloudflare(deps: Deps) -> tuple[CloudflareClient, str]:
    """The Cloudflare client and zone; on the first run they are asked, checked and saved."""
    values = localconf.load(deps.conf_path)
    token, zone = values.get("CLOUDFLARE_API_TOKEN"), values.get("CLOUDFLARE_ZONE")
    if token and zone:
        return deps.make_cf(token), zone
    ui = deps.ui
    ui.info("第一次运行：需要一个 Cloudflare API token，以及一个托管在 Cloudflare 上的域名。这一步可以开着代理。")
    token = token or ui.secret("Cloudflare API token")
    zone = zone or ui.ask("要使用的域名（例如 example.com）", validate=_zone_error).rstrip(".").lower()
    cf = deps.make_cf(token)
    missing = check_token(cf, zone)
    if missing:
        raise CnsrError(f"Cloudflare token 缺少这些权限：{'、'.join(missing)}。请在 Cloudflare 后台补上后重跑。")
    localconf.save(deps.conf_path, {**localconf.load(deps.conf_path), "CLOUDFLARE_API_TOKEN": token, "CLOUDFLARE_ZONE": zone})
    ui.detail(f"Cloudflare 设置已保存到 {deps.conf_path}。")
    return cf, zone


def _alias(ns: Namespace, deps: Deps) -> str:
    if ns.alias is None:
        return deps.ui.ask("节点别名（例如 jp-1）", validate=validate_alias)
    error = validate_alias(ns.alias)
    if error is not None:
        raise CnsrError(error)
    return ns.alias


def _check_static_ip(ns: Namespace) -> None:
    if ns.static_ip is None:
        return
    if not ns.aws:
        raise CnsrError("--static-ip 只能和 --aws 一起用：它是你在这个 AWS 区域里已有的静态 IP 的地址。")
    if _ipv4_error(ns.static_ip) is not None:
        raise CnsrError(f"--static-ip 的 {ns.static_ip} 不是 IPv4 地址。")


def _rerun_request(ns: Namespace, node: Node) -> AddRequest:
    """The request for a node the inventory knows; the arguments may only repeat it."""
    if (
        (ns.aws and (node.source != "aws" or ns.aws != node.region))
        or (ns.ip and (node.source != "vps" or ns.ip != node.ipv4))
        or (ns.static_ip and (not node.static_ip_reused or ns.static_ip != node.ipv4))
    ):
        where = f"AWS {node.region}" if node.source == "aws" else f"已有服务器 {node.ipv4}"
        raise CnsrError(
            f"节点 {node.alias} 已存在（{where}），和这次的参数不一致。"
            f"要部署到别处，请换一个别名，或先运行 cnsr rm {node.alias}。"
        )
    default_user = LOGIN_USER if node.source == "aws" else "root"
    user, port = (node.ssh.user, node.ssh.port) if node.ssh else (default_user, 22)
    ip = node.ipv4 if node.source == "vps" else None
    return AddRequest(node.alias, node.source, node.region, ip, user, port, static_ip=ns.static_ip)


def _new_request(ns: Namespace, deps: Deps, alias: str, block: HostBlock | None) -> AddRequest:
    """The request for a node the inventory does not know.

    An alias in ``~/.ssh/config`` that already names a host is used only when it names the
    server to take over; otherwise it belongs to another machine and the run stops here.
    """
    ui = deps.ui
    source = "aws" if ns.aws else "vps" if ns.ip else SOURCES[ui.choose("服务器来源", SOURCE_LABELS)]
    named = block.hostname if block is not None else ""
    if source == "aws":
        if named:
            raise _foreign_alias(deps, alias, named, "而这次要在 AWS Lightsail 新开一台实例")
        codes = list(REGIONS)
        region = ns.aws or codes[ui.choose("AWS 区域", [REGION_LABELS.get(code, code) for code in codes])]
        return AddRequest(alias, "aws", region, None, LOGIN_USER, 22, static_ip=ns.static_ip)
    if ns.ip is not None and (error := _ipv4_error(ns.ip)) is not None:
        raise CnsrError(error)
    ip = ns.ip
    if ip is None and named and _ipv4_error(named) is None:
        ip = named  # the alias already says which server
    if named and named != ip:
        reason = f"不是这次要接管的服务器 {ip}" if ip else "不是 IPv4 地址，无法确认它就是这次要接管的服务器"
        raise _foreign_alias(deps, alias, named, reason)
    if block is not None and _logs_in_to(block, ip):
        return AddRequest(alias, "vps", None, block.hostname, block.user, block.port)
    ip = ip or ui.ask("服务器的 IPv4 地址", validate=_ipv4_error)
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
    if not block.user or _ipv4_error(block.hostname) is not None:
        return False
    return ip is None or block.hostname == ip


def _lightsail(deps: Deps, region: str) -> Lightsail:
    """Lightsail for ``region``: boto3's default credentials first, then saved or typed keys.

    Typed keys are saved only after a read-only call shows that they work.
    """
    known = known_lightsail(deps, region)
    if known is not None:
        return known
    ui = deps.ui
    ui.info("没有找到 AWS 凭据（环境变量和 ~/.aws 里都没有）。请输入用于 Lightsail 的 access key，cnsr 会把它保存在本机。")
    keys = (ui.ask("AWS access key ID", sensitive=True), ui.secret("AWS secret access key"))
    lightsail = deps.make_lightsail(region, keys)
    lightsail.pick_blueprint()
    values = localconf.load(deps.conf_path)
    localconf.save(deps.conf_path, {**values, "AWS_ACCESS_KEY_ID": keys[0], "AWS_SECRET_ACCESS_KEY": keys[1]})
    return lightsail


def _key_name_hint(alias: str, existing: Node | None, block: HostBlock | None) -> str:
    """The key name to look for: the inventory's, else the alias's public key file, else the alias."""
    if existing is not None and existing.ssh is not None:
        return existing.ssh.key_name
    if block is not None and block.identity_file.endswith(".pub"):
        return re.split(r"[\\/]", block.identity_file)[-1].removesuffix(".pub") or alias
    return alias


def _zone_error(value: str) -> str | None:
    if _ZONE.fullmatch(value.rstrip(".").lower()):
        return None
    return "请输入域名本身，例如 example.com，不带 https:// 和路径。"


def _ipv4_error(value: str) -> str | None:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return f"{value} 不是 IPv4 地址，请输入服务器的公网 IPv4 地址。"
    return None


def _user_error(value: str) -> str | None:
    return None if re.fullmatch(r"[a-z_][a-z0-9_.-]*", value) else "用户名只能包含小写字母、数字、下划线、点和连字符。"


def _port_error(value: str) -> str | None:
    return None if value.isdecimal() and 1 <= int(value) <= 65535 else "端口必须是 1 到 65535 之间的数字。"


def _print_summary(ctx: DeployContext) -> None:
    node, ui = ctx.node, ctx.deps.ui
    ui.info("")
    ui.info(f"节点 {node.alias} 已就绪：{node.domain}，SNI {node.xray.sni}。")
    if ctx.alias_switched:
        ui.info(f"SSH 别名 {node.alias} 已指向 {node.domain}，可以直接用 ssh {node.alias} 登录。")
    else:
        ui.warn(f"SSH 别名 {node.alias} 暂时仍指向 IP {node.ipv4}；以后运行 cnsr add {node.alias} 时会再检查并切到域名。")
    ui.info("")
    ui.info("圈X 节点行（控制端的远程资源 cnsr 里已经有它）：")
    ui.info(node.line)
    ui.info("")
    ui.info("每台设备在圈X 配置里粘贴一次下面几行；分享节点时，把同样几行发给对方：")
    ui.info(client_lines(ctx.meta))
    ui.info("")
    ui.info(f"再把 cnsr 加进策略组的 resource-tag-regex，例如 {TAG_REGEX_EXAMPLE}。")
    ui.info("")
    ui.info("用 v2rayN 的设备添加下面这个订阅地址，并设自动更新（建议 60 分钟），不要勾选通过代理更新：")
    ui.info(v2rayn_url(ctx.meta))
    ui.info("v2rayN 不参与检测：SNI 被封时由圈X 设备发现并换好，v2rayN 下次更新订阅时跟上。")
