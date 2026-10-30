"""``cnsr up``: gather what the deployment needs, run the pipeline, print what to do next.

Cloudflare is asked first (first run only); the other questions, which a menu's "back" can
return to, are in ``cnsr.commands.up_questions``.
"""

from __future__ import annotations

import re
import shlex
from argparse import Namespace

from cnsr import features, localconf
from cnsr.cloudflare.api import CloudflareClient
from cnsr.cloudflare.control import check_token, ensure_control_plane
from cnsr.deploy import static_ip
from cnsr.commands import size_menu, up_questions
from cnsr.commands.up_questions import TO_FEATURES, ipv4_error
from cnsr.commands.rm import remove_node
from cnsr.commands.token_hint import saved_token_hint
from cnsr.deploy.context import AddRequest, DeployContext, Deps, default_deps
from cnsr.deploy.steps import run_pipeline
from cnsr.errors import CnsrError
from cnsr.qx import client_lines, v2rayn_url
from cnsr.state import Inventory, Node, SshInfo, is_cloud

# What the cleanup after a failed first deployment deletes, by cloud: the vendor and its resources.
CLEANUP_WHAT = {"aws": ("AWS", "实例、静态 IP"), "hetzner": ("Hetzner", "服务器、Primary IP、防火墙")}
TAG_REGEX_EXAMPLE = "^(self-built|cnsr)$"
_ZONE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def run(ns: Namespace, deps: Deps | None = None) -> int:
    """Deploy the server ``ns.alias``, or bring it up to date; 0 on success, 1 after an error.

    The error is printed here. Ctrl-C and unexpected errors go up to ``cnsr.cli``, which reports
    them. In every case, a first cloud deployment that did not finish first deletes what it created.
    ``ns.features``, when set, are the features to deploy instead of asking (the tests use it);
    ``ns.to``, when set, names the last of them (``--to``).
    """
    deps = deps or default_deps()
    ctx: DeployContext | None = None
    try:
        with saved_token_hint(deps.conf_path):
            ctx = prepare(ns, deps)
            run_pipeline(ctx)
            static_ip.hand_over(ctx)
            ctx.node.features = list(ctx.features)
            ctx.save()
            ctx.inventory.keep_indexed(ctx.node.alias)
    except CnsrError as exc:
        deps.ui.error(str(exc))
        _clean_up(ctx)
        return 1
    except (KeyboardInterrupt, Exception):  # Ctrl-C or an unexpected error: ``cnsr.cli`` reports it
        _clean_up(ctx)
        raise
    _print_summary(ctx)
    static_ip.remind(ctx)
    return 0


def _clean_up(ctx: DeployContext | None) -> None:
    """Delete what a first cloud deployment that stopped part way created, so nothing keeps costing.

    A server a finished run left anything on is in use and stays as it is, and so does a vendor
    server, which is not cnsr's to delete. After the cleanup the next run starts from scratch.
    """
    if ctx is None or not is_cloud(ctx.req.source) or ctx.in_use:
        return
    ui, node = ctx.deps.ui, ctx.node
    alias = node.alias
    # rm keeps a reused static IP: it goes back to the instance it was moved from
    moved = (node.static_ip_name, node.static_ip_from) if node.static_ip_reused else (None, None)
    vendor, resources = CLEANUP_WHAT[ctx.req.source]
    cleaning = f"部署没有完成，删除这次在 {vendor} 和 Cloudflare 上建的{resources}、DNS 记录和服务器登记"
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
    static_ip.restore(ctx)
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
    _check_size(ns)
    cf, zone = _cloudflare(deps)
    ui.busy("检查 Cloudflare 控制端")
    ui.detail("检查 Cloudflare 控制端：没有就部署，已有就更新到当前版本。")
    try:
        kv, meta, zone_id = ensure_control_plane(cf, zone, deps.rng)
        deps.check_control(meta)  # before anything is created: a blocked control plane is useless
    finally:
        ui.idle()
    inventory = Inventory(kv)
    a = up_questions.answers(ns, deps, inventory)
    alias, existing, req, chosen, installed = a.alias, a.existing, a.req, a.chosen, a.installed
    lightsail, hetzner, offer, agent, key, key_name = a.lightsail, a.hetzner, a.offer, a.agent, a.key, a.key_name
    if existing is None:
        ui.info(f"等价命令（换一个别名即可按同样的配置再开一台）：{_equivalent(req, chosen)}")
        # A device's report right after an earlier rm may have left a rotation state behind,
        # which would send the new node straight into a rotation or a pause.
        inventory.delete_state(alias)
    saved = existing.to_json() if existing is not None else None
    node = existing or Node(alias=alias, source=req.source, ipv4="", region=req.region, size=req.size)
    node.ssh = SshInfo(req.user, req.port, key_name)
    return DeployContext(
        req, deps, node, inventory, cf, zone_id, zone, meta, key, agent, features.app_ports(chosen),
        features=chosen, in_use=bool(installed), lightsail=lightsail, hetzner=hetzner, offer=offer, saved=saved,
        deployed=existing is not None and existing.line is not None,
    )  # fmt: skip


def _equivalent(req: AddRequest, chosen: list[str]) -> str:
    """The ``cnsr up`` command line that deploys a new server the way ``req`` and ``chosen`` say.

    It holds no token, password or key: those stay where this run read them from. Nor does it
    hold ``--static-ip``: a static IP serves one node, so another alias would only be refused it.
    """
    words = ["cnsr", "up", req.alias]
    if is_cloud(req.source):
        words += [f"--{req.source}", req.region, "--size", req.size]
    else:
        words += ["--ip", req.ip]
    last = [name for name, feature in TO_FEATURES.items() if feature in chosen][-1]
    words += ["--to", last]
    return shlex.join(words)


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


def _check_static_ip(ns: Namespace) -> None:
    if ns.static_ip is None:
        return
    if not ns.aws:
        raise CnsrError("--static-ip 只能和 --aws 一起用：它是你在这个 AWS 区域里已有的静态 IP 的地址。")
    if ipv4_error(ns.static_ip) is not None:
        raise CnsrError(f"--static-ip 的 {ns.static_ip} 不是 IPv4 地址。")


def _check_size(ns: Namespace) -> None:
    if ns.size is None:
        return
    if not (ns.aws or ns.hetzner):
        raise CnsrError("--size 只能和 --aws 或 --hetzner 一起用：它是新开云服务器的规格。")
    if (error := size_menu.check(ns.size, "hetzner" if ns.hetzner else "aws")) is not None:
        raise CnsrError(error)


def _zone_error(value: str) -> str | None:
    if _ZONE.fullmatch(value.rstrip(".").lower()):
        return None
    return "请输入域名本身，例如 example.com，不带 https:// 和路径。"


def _print_summary(ctx: DeployContext) -> None:
    node, ui = ctx.node, ctx.deps.ui
    ui.info("")
    if features.XRAY not in ctx.features:
        ssh = node.ssh
        ui.info(f"服务器 {node.alias} 已加固：用 ssh {node.alias} 登录（用户 {ssh.user}，端口 {ssh.port}）。")
        ui.info(f"以后要在这台服务器上部署 Xray 节点，运行 cnsr up {node.alias} 并勾选它。")
        return
    ui.info(f"节点 {node.alias} 已就绪：{node.domain}，SNI {node.xray.sni}。")
    if ctx.alias_switched:
        ui.info(f"SSH 别名 {node.alias} 已指向 {node.domain}，可以直接用 ssh {node.alias} 登录。")
    else:
        ui.warn(f"SSH 别名 {node.alias} 暂时仍指向 IP {node.ipv4}；以后运行 cnsr up {node.alias} 时会再检查并切到域名。")
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
