"""Deploy or adopt the control-plane Worker and probe node ports through it."""

from __future__ import annotations

import json
import random
import secrets
from dataclasses import dataclass
from importlib import resources
from typing import Any

import httpx

from cnsr import __version__
from cnsr.cloudflare.api import CloudflareClient, CloudflareError
from cnsr.cloudflare.dns import fresh_name
from cnsr.cloudflare.kv import KvStore
from cnsr.errors import CnsrError
from cnsr.state import ControlMeta, Inventory

WORKER_NAME = "cnsr-control"
KV_TITLE = "cnsr-control"
COMPAT_DATE = "2026-09-01"

KV_BINDING = "CNSR"
MODULE_TYPE = "application/javascript+module"
TEXT_TYPE = "text/plain"
# (upload name, package, file in the package, content type)
MODULES = (
    ("worker.js", "cnsr.assets.worker", "worker.js", MODULE_TYPE),
    ("handler.js", "cnsr.assets.worker", "handler.js", MODULE_TYPE),
    ("common.js", "cnsr.assets.worker", "common.js", MODULE_TYPE),
    ("store.js", "cnsr.assets.worker", "store.js", MODULE_TYPE),
    ("node-api.js", "cnsr.assets.worker", "node-api.js", MODULE_TYPE),
    ("client-api.js", "cnsr.assets.worker", "client-api.js", MODULE_TYPE),
    ("v2rayn.js", "cnsr.assets.worker", "v2rayn.js", MODULE_TYPE),
    ("qx-check.txt", "cnsr.assets.qx", "qx-check.js", TEXT_TYPE),
    ("qx-check-ui.txt", "cnsr.assets.qx", "qx-check-ui.js", TEXT_TYPE),
)
NAMESPACES_PER_PAGE = 100
# The Worker gives up on a connection after 5 seconds; leave room for the round trip.
PROBE_TIMEOUT_SECONDS = 10.0
# Cloudflare's error code for an account without a workers.dev subdomain (as wrangler reads it).
_NO_SUBDOMAIN = 10007
# Cloudflare sets this on the challenge or block pages it serves instead of the Worker's answer.
_MITIGATED = "cf-mitigated"
# Cloudflare's browser integrity check challenges python-httpx's default user agent.
USER_AGENT = f"cnsr/{__version__}"

_PROBE_STATUS_TEXT = {
    400: "控制端不接受这次探测的地址或端口",
    401: "控制端拒绝了管理 token，请重新运行 add 更新控制端",
    404: "控制端没有探测接口，可能还没更新到当前版本，请重新运行 add",
    405: "控制端不支持这种探测请求方式，请重新运行 add 更新控制端",
    503: "控制端尚未配置完成，请重新运行 add",
}


class ControlPlaneBlocked(CnsrError):
    """Cloudflare answers for the control plane with a challenge or block page of its own."""

    def __init__(self, host: str, verdict: str):
        super().__init__(
            f"Cloudflare 拦下了发往控制端 {host} 的请求：它返回的是浏览器质询页（cf-mitigated: {verdict}），"
            "cnsr 和圈X 都过不了。请在 Cloudflare 后台打开这个域名的“安全性 → 事件”，按主机名 "
            f"{host} 查看是哪项设置拦的，并只对这个主机名关掉它：WAF 自定义规则加排除或跳过规则；"
            "安全级别用配置规则调低；Bot Fight Mode 只能整个域名关闭。改好后重跑同一条命令。"
        )


@dataclass
class ProbeResult:
    """One outside connection attempt. ``control_reached`` is False when this machine could not
    reach the control plane at all, so the node was never tried."""

    ok: bool
    ms: int | None
    error: str | None
    control_reached: bool = True


def check_token(cf: CloudflareClient, zone_name: str) -> list[str]:
    """Return the Chinese names of the permissions the token lacks.

    Each permission is checked with one read-only call, so a missing permission
    is a 401 or 403 on that call; any other failure propagates. Without access
    to the zone the remaining checks cannot be addressed, so the list then holds
    only the zone permission.
    """
    try:
        zone = cf.zone(zone_name)
    except CloudflareError as exc:
        if _is_permission_error(exc):
            return ["读取域名"]
        raise
    zone_id, account = zone["id"], f"/accounts/{zone['account']['id']}"
    probes: list[tuple[str, str, dict[str, Any]]] = [
        ("编辑 DNS", f"/zones/{zone_id}/dns_records", {"per_page": 1}),
        ("编辑 Workers 脚本", f"{account}/workers/scripts", {}),
        ("编辑 Workers KV", f"{account}/storage/kv/namespaces", {"per_page": 1}),
        ("绑定 Workers 自定义域名", f"{account}/workers/domains", {"zone_id": zone_id}),
    ]
    missing = []
    for permission, path, params in probes:
        try:
            cf.request("GET", path, params=params)
        except CloudflareError as exc:
            if not _is_permission_error(exc):
                raise
            missing.append(permission)
    return missing


def ensure_control_plane(cf: CloudflareClient, zone_name: str, rng: random.Random) -> tuple[KvStore, ControlMeta, str]:
    """Create or adopt the control plane and bring its Worker up to date.

    Safe to rerun: an existing KV namespace and ``meta:control`` are reused, so
    the host and tokens never change. A new host is a random name that has no DNS
    record yet. New metadata is saved before the Worker is uploaded or bound, so a
    run that fails midway is completed by the next one.
    Existing metadata that belongs to another zone is refused with ``CnsrError``
    before anything is uploaded or bound. Returns the KV store, the metadata and
    the zone id.

    The Worker is also served on workers.dev, and the metadata records that address as
    ``node_host``: the nodes call it there, because zone security settings such as Bot Fight
    Mode challenge requests from cloud providers' addresses, and they do not apply to workers.dev.
    """
    zone = cf.zone(zone_name)
    zone_id, account_id = zone["id"], zone["account"]["id"]
    namespace_id = _ensure_namespace(cf, account_id)
    kv = KvStore(cf, account_id, namespace_id)
    inventory = Inventory(kv)
    meta = inventory.meta()
    if meta is None:
        meta = ControlMeta(
            host=fresh_name(cf, zone_id, zone_name, rng),
            zone=zone_name,
            client_token=secrets.token_urlsafe(24),
            admin_token=secrets.token_urlsafe(24),
        )
        inventory.put_meta(meta)
    else:
        _check_zone(meta, zone_name)
    _upload_worker(cf, account_id, namespace_id)
    cf.request(
        "PUT",
        f"/accounts/{account_id}/workers/domains",
        json={"environment": "production", "hostname": meta.host, "service": WORKER_NAME, "zone_id": zone_id},
    )
    node_host = _enable_workers_dev(cf, account_id)
    if meta.node_host != node_host:
        meta.node_host = node_host
        inventory.put_meta(meta)
    return kv, meta, zone_id


def find_control_plane(cf: CloudflareClient, zone_name: str) -> tuple[KvStore, ControlMeta, str] | None:
    """Look up the control plane that ``ensure_control_plane`` made, changing nothing.

    Returns the KV store, the metadata and the zone id, or None when there is no KV
    namespace or no metadata yet. Metadata that belongs to another zone is refused with
    ``CnsrError``, as ``ensure_control_plane`` does.
    """
    zone = cf.zone(zone_name)
    zone_id, account_id = zone["id"], zone["account"]["id"]
    namespace_id = _find_namespace(cf, account_id)
    if namespace_id is None:
        return None
    kv = KvStore(cf, account_id, namespace_id)
    meta = Inventory(kv).meta()
    if meta is None:
        return None
    _check_zone(meta, zone_name)
    return kv, meta, zone_id


def probe(meta: ControlMeta, host: str, port: int = 443, transport: httpx.BaseTransport | None = None) -> ProbeResult:
    """Ask the control plane to try a TCP connection to ``host``:``port`` from outside.

    A connection that fails, from the Worker's side or on the way to it, gives
    ``ok=False`` with a Chinese reason; on the way to it also ``control_reached=False``.
    A reply that is not a proper answer (wrong token, outdated Worker, ...) raises
    ``CnsrError``.
    """
    try:
        with httpx.Client(transport=transport, timeout=PROBE_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
            response = client.post(
                f"https://{meta.host}/admin/probe",
                headers={"Authorization": f"Bearer {meta.admin_token}"},
                json={"host": host, "port": port},
            )
    except httpx.TransportError as exc:
        reason = "请求控制端超时" if isinstance(exc, httpx.TimeoutException) else "无法连接控制端"
        return ProbeResult(ok=False, ms=None, error=reason, control_reached=False)
    if verdict := response.headers.get(_MITIGATED):
        raise ControlPlaneBlocked(meta.host, verdict)
    if response.status_code != 200:
        detail = _PROBE_STATUS_TEXT.get(response.status_code, "控制端返回了意外的响应")
        raise CnsrError(f"{detail}（HTTP {response.status_code}）。")
    return _parse_probe(response)


def check_reachable(meta: ControlMeta, transport: httpx.BaseTransport | None = None) -> None:
    """Raise ``ControlPlaneBlocked`` when Cloudflare challenges or blocks requests to the control plane.

    Anything else passes, a connection error included: a control plane deployed seconds ago may
    still be waiting for its certificate, which the outside probe in step 6 waits for.
    """
    try:
        with httpx.Client(transport=transport, timeout=PROBE_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(f"https://{meta.host}/")
    except httpx.TransportError:
        return
    if verdict := response.headers.get(_MITIGATED):
        raise ControlPlaneBlocked(meta.host, verdict)


def _zone_key(zone: str) -> str:
    return zone.rstrip(".").lower()


def _is_permission_error(exc: CloudflareError) -> bool:
    return exc.status in (401, 403)


def _check_zone(meta: ControlMeta, zone_name: str) -> None:
    if _zone_key(meta.zone) != _zone_key(zone_name):
        raise CnsrError(
            f"控制端已经部署在 {meta.zone} 下，与当前设置的域名 {zone_name} 不一致。"
            f"请把本机配置里的 CLOUDFLARE_ZONE 改回 {meta.zone}；"
            f"如果确实要换域名，先在 Cloudflare 里删除名为 {WORKER_NAME} 的 KV 命名空间和 Worker，再重新运行。"
        )


def _namespaces_path(account_id: str) -> str:
    return f"/accounts/{account_id}/storage/kv/namespaces"


def _ensure_namespace(cf: CloudflareClient, account_id: str) -> str:
    found = _find_namespace(cf, account_id)
    if found is not None:
        return found
    return cf.request("POST", _namespaces_path(account_id), json={"title": KV_TITLE})["id"]


def _find_namespace(cf: CloudflareClient, account_id: str) -> str | None:
    """The id of the control plane's KV namespace, or None when the account has none."""
    page = 1
    while True:
        found = cf.request("GET", _namespaces_path(account_id), params={"page": page, "per_page": NAMESPACES_PER_PAGE})
        for namespace in found:
            if namespace["title"] == KV_TITLE:
                return namespace["id"]
        if len(found) < NAMESPACES_PER_PAGE:
            return None
        page += 1


def _upload_worker(cf: CloudflareClient, account_id: str, namespace_id: str) -> None:
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": COMPAT_DATE,
        "bindings": [{"type": "kv_namespace", "name": KV_BINDING, "namespace_id": namespace_id}],
    }
    files: dict[str, tuple[str | None, str, str]] = {"metadata": (None, json.dumps(metadata), "application/json")}
    for name, package, filename, content_type in MODULES:
        source = (resources.files(package) / filename).read_text(encoding="utf-8-sig")
        files[name] = (name, source, content_type)
    cf.request("PUT", f"/accounts/{account_id}/workers/scripts/{WORKER_NAME}", files=files)


def _enable_workers_dev(cf: CloudflareClient, account_id: str) -> str:
    """Serve the uploaded Worker on workers.dev and return that host.

    The account needs its workers.dev subdomain first, as wrangler does it.
    """
    subdomain = _workers_subdomain(cf, account_id)
    cf.request(
        "POST",
        f"/accounts/{account_id}/workers/scripts/{WORKER_NAME}/subdomain",
        json={"enabled": True, "previews_enabled": False},
    )
    return f"{WORKER_NAME}.{subdomain}.workers.dev"


def _workers_subdomain(cf: CloudflareClient, account_id: str) -> str:
    """The account's <name>.workers.dev name, set up with a random name when it has none yet."""
    path = f"/accounts/{account_id}/workers/subdomain"
    try:
        return cf.request("GET", path)["subdomain"]
    except CloudflareError as exc:
        if _NO_SUBDOMAIN not in exc.codes and exc.status != 404:
            raise
    return cf.request("PUT", path, json={"subdomain": f"cnsr-{secrets.token_hex(4)}"})["subdomain"]


def _parse_probe(response: httpx.Response) -> ProbeResult:
    unreadable = CnsrError("控制端返回了无法识别的探测结果，请重新运行 add 更新控制端。")
    try:
        body = response.json()
    except ValueError:
        raise unreadable from None
    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
        raise unreadable
    ms, error = body.get("ms"), body.get("error")
    return ProbeResult(
        ok=body["ok"],
        ms=ms if isinstance(ms, int) else None,
        error=None if body["ok"] else _describe_probe_error(error),
    )


def _describe_probe_error(error: object) -> str:
    """One of a few fixed Chinese texts for the Worker's English failure text, never that text."""
    text = error.lower() if isinstance(error, str) else ""
    if text == "timeout":
        return "连接超时"
    if "refused" in text:
        return "连接被拒绝"
    return "连接失败"
