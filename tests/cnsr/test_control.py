import json
import random
import re
from pathlib import Path

import pytest

import cnsr
from cnsr import naming
from cnsr.cloudflare import control
from cnsr.cloudflare.api import CloudflareError
from cnsr.cloudflare.kv import KvStore
from cnsr.errors import CnsrError
from cnsr.state import ControlMeta

ACCOUNT = "acct-1"
ZONE_ID = "zone-1"
SCRIPT_PATH = f"/accounts/{ACCOUNT}/workers/scripts/cnsr-control"
DOMAINS_PATH = f"/accounts/{ACCOUNT}/workers/domains"
NAMESPACES_PATH = f"/accounts/{ACCOUNT}/storage/kv/namespaces"
WORKER_DIR = Path(cnsr.__file__).parent / "assets" / "worker"


def stored_meta(mock_cf, namespace_id="ns-1") -> dict:
    return json.loads(mock_cf.kv[namespace_id]["meta:control"])


def parts_by_name(mock_cf) -> dict:
    return {part.name: part for part in mock_cf.scripts["cnsr-control"]}


# --- constants ---


def test_constants():
    assert control.WORKER_NAME == "cnsr-control"
    assert control.KV_TITLE == "cnsr-control"
    assert control.COMPAT_DATE == "2026-09-01"


# --- check_token ---


def test_check_token_returns_nothing_when_every_permission_is_present(mock_cf):
    assert control.check_token(mock_cf.client, "example.com") == []
    assert mock_cf.calls() == [
        ("GET", "/zones"),
        ("GET", f"/zones/{ZONE_ID}/dns_records"),
        ("GET", f"/accounts/{ACCOUNT}/workers/scripts"),
        ("GET", NAMESPACES_PATH),
        ("GET", DOMAINS_PATH),
    ]


def test_check_token_probes_never_write(mock_cf):
    control.check_token(mock_cf.client, "example.com")
    assert {method for method, _ in mock_cf.calls()} == {"GET"}


def test_check_token_reports_missing_workers(mock_cf):
    mock_cf.deny("GET", f"/accounts/{ACCOUNT}/workers/scripts", 403)
    assert control.check_token(mock_cf.client, "example.com") == ["编辑 Workers 脚本"]


@pytest.mark.parametrize(
    "path,name",
    [
        (f"/zones/{ZONE_ID}/dns_records", "编辑 DNS"),
        (f"/accounts/{ACCOUNT}/workers/scripts", "编辑 Workers 脚本"),
        (NAMESPACES_PATH, "编辑 Workers KV"),
        (DOMAINS_PATH, "绑定 Workers 自定义域名"),
    ],
)
@pytest.mark.parametrize("status", [401, 403])
def test_check_token_maps_each_probe_to_its_permission(mock_cf, path, name, status):
    mock_cf.deny("GET", path, status)
    assert control.check_token(mock_cf.client, "example.com") == [name]


def test_check_token_lists_every_missing_permission_in_probe_order(mock_cf):
    mock_cf.deny("GET", DOMAINS_PATH, 403)
    mock_cf.deny("GET", NAMESPACES_PATH, 403)
    mock_cf.deny("GET", f"/zones/{ZONE_ID}/dns_records", 401)
    assert control.check_token(mock_cf.client, "example.com") == ["编辑 DNS", "编辑 Workers KV", "绑定 Workers 自定义域名"]


def test_check_token_without_zone_read_stops_at_the_first_permission(mock_cf):
    mock_cf.deny("GET", "/zones", 403)
    assert control.check_token(mock_cf.client, "example.com") == ["读取域名"]
    assert mock_cf.calls() == [("GET", "/zones")]


def test_check_token_propagates_other_cloudflare_errors(mock_cf):
    mock_cf.deny("GET", NAMESPACES_PATH, 500)
    with pytest.raises(CloudflareError) as info:
        control.check_token(mock_cf.client, "example.com")
    assert info.value.status == 500


def test_check_token_propagates_an_unknown_zone(mock_cf):
    with pytest.raises(CnsrError) as info:
        control.check_token(mock_cf.client, "other.example.org")
    assert "other.example.org" in str(info.value)


# --- ensure_control_plane ---


def test_ensure_control_plane_fresh_account(mock_cf):
    kv, meta, zone_id = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert zone_id == ZONE_ID
    assert isinstance(kv, KvStore)
    assert list(mock_cf.namespaces.values()) == ["cnsr-control"]
    (namespace_id,) = mock_cf.namespaces

    create = next(r for r in mock_cf.requests if (r.method, r.url.path.removeprefix("/client/v4")) == ("POST", NAMESPACES_PATH))
    assert json.loads(create.content) == {"title": "cnsr-control"}

    parts = parts_by_name(mock_cf)
    assert set(parts) == {"metadata", *(module[0] for module in control.MODULES)}
    assert json.loads(parts["metadata"].body) == {
        "main_module": "worker.js",
        "compatibility_date": "2026-09-01",
        "bindings": [{"type": "kv_namespace", "name": "CNSR", "namespace_id": namespace_id}],
    }
    assert mock_cf.domains == [
        {"environment": "production", "hostname": meta.host, "service": "cnsr-control", "zone_id": ZONE_ID}
    ]

    assert meta.host.endswith(".example.com")
    assert meta.zone == "example.com"
    assert stored_meta(mock_cf) == {
        "host": meta.host,
        "zone": "example.com",
        "client_token": meta.client_token,
        "admin_token": meta.admin_token,
        "node_host": "cnsr-control.acct-sub.workers.dev",
    }
    assert kv.get("meta:control") == mock_cf.kv[namespace_id]["meta:control"]


def test_ensure_control_plane_builds_the_host_from_the_random_label(mock_cf):
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert meta.host == f"{naming.random_label(random.Random(1))}.example.com"
    assert re.fullmatch(r"[a-z]+-[a-z]+-v\d{3}\.example\.com", meta.host)


def test_ensure_control_plane_never_takes_a_host_name_that_already_has_records(mock_cf):
    rng = random.Random(1)
    taken, free = (f"{naming.random_label(rng)}.example.com" for _ in range(2))
    mock_cf.dns_names.add(taken)
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert meta.host == free and stored_meta(mock_cf)["host"] == free
    calls = mock_cf.calls()
    lookups = [i for i, call in enumerate(calls) if call == ("GET", f"/zones/{ZONE_ID}/dns_records")]
    meta_write = next(i for i, (method, path) in enumerate(calls) if method == "PUT" and path.endswith("/values/meta:control"))
    assert len(lookups) == 2 and lookups[-1] < meta_write
    assert [dict(r.url.params) for r in mock_cf.requests if r.url.path.endswith("/dns_records")] == [
        {"name": taken}, {"name": free},
    ]  # fmt: skip


def test_ensure_control_plane_does_not_look_up_the_host_of_an_existing_control_plane(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    existing = {"host": "api-sync-v042.example.com", "zone": "example.com", "client_token": "ct", "admin_token": "at"}
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps(existing)
    mock_cf.dns_names.add("api-sync-v042.example.com")  # its own Worker record
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert meta.host == "api-sync-v042.example.com"
    assert ("GET", f"/zones/{ZONE_ID}/dns_records") not in mock_cf.calls()


def test_ensure_control_plane_generates_two_distinct_urlsafe_tokens(mock_cf):
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert meta.client_token != meta.admin_token
    for token in (meta.client_token, meta.admin_token):
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", token)


def test_ensure_control_plane_tokens_are_not_derived_from_the_seeded_rng(mock_cf):
    _, first, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    mock_cf.kv["ns-1"].clear()
    _, second, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert second.host == first.host
    assert second.client_token != first.client_token
    assert second.admin_token != first.admin_token


def test_ensure_control_plane_uploads_the_packaged_sources_as_modules(mock_cf):
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    parts = parts_by_name(mock_cf)
    assert parts["metadata"].content_type == "application/json"
    for name in ("worker.js", "handler.js"):
        assert parts[name].filename == name
        assert parts[name].content_type == "application/javascript+module"
        assert parts[name].body == (WORKER_DIR / name).read_bytes()
    assert "package.json" not in parts


def test_every_worker_module_in_the_package_is_uploaded():
    names = {module[2] for module in control.MODULES if module[1] == "cnsr.assets.worker"}
    assert names == {path.name for path in WORKER_DIR.glob("*.js")}


def test_every_worker_module_is_uploaded_with_its_type(mock_cf):
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    parts = parts_by_name(mock_cf)
    js = "application/javascript+module"
    assert {name: part.content_type for name, part in parts.items() if name != "metadata"} == {
        "worker.js": js,
        "handler.js": js,
        "common.js": js,
        "store.js": js,
        "node-api.js": js,
        "client-api.js": js,
        "v2rayn.js": js,
        "qx-check.txt": "text/plain",
        "qx-check-ui.txt": "text/plain",
    }
    qx_dir = WORKER_DIR.parent / "qx"
    assert parts["qx-check.txt"].body == (qx_dir / "qx-check.js").read_bytes()
    assert parts["qx-check-ui.txt"].body == (qx_dir / "qx-check-ui.js").read_bytes()


def test_ensure_control_plane_puts_the_script_under_the_worker_name(mock_cf):
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert list(mock_cf.scripts) == ["cnsr-control"]
    assert ("PUT", SCRIPT_PATH) in mock_cf.calls()


def test_ensure_control_plane_saves_meta_before_uploading_or_binding(mock_cf):
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    calls = mock_cf.calls()
    meta_write = next(i for i, (method, path) in enumerate(calls) if method == "PUT" and path.endswith("/values/meta:control"))
    assert meta_write < calls.index(("PUT", SCRIPT_PATH)) < calls.index(("PUT", DOMAINS_PATH))


@pytest.mark.parametrize("failing", [SCRIPT_PATH, DOMAINS_PATH])
def test_ensure_control_plane_rerun_after_partial_failure_reuses_host_and_tokens(mock_cf, failing):
    mock_cf.deny("PUT", failing, 500)
    with pytest.raises(CloudflareError):
        control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    before = stored_meta(mock_cf)

    mock_cf.allow("PUT", failing)
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(2))

    assert stored_meta(mock_cf) == {**before, "node_host": "cnsr-control.acct-sub.workers.dev"}
    assert {"host": meta.host, "zone": meta.zone, "client_token": meta.client_token, "admin_token": meta.admin_token, "node_host": ""} == before
    assert mock_cf.domains[-1]["hostname"] == before["host"]
    assert len(mock_cf.namespaces) == 1


def test_ensure_control_plane_adopts_existing(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    existing = {"host": "api-sync-v042.example.com", "zone": "example.com", "client_token": "ct", "admin_token": "at", "node_host": "cnsr-control.acct-sub.workers.dev"}
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps(existing)

    kv, meta, zone_id = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert meta == ControlMeta("api-sync-v042.example.com", "example.com", "ct", "at", "cnsr-control.acct-sub.workers.dev")
    assert zone_id == ZONE_ID
    assert kv.get("meta:control") == json.dumps(existing)
    calls = mock_cf.calls()
    assert ("POST", NAMESPACES_PATH) not in calls
    assert not [c for c in calls if c[0] == "PUT" and "/values/" in c[1]]
    assert ("PUT", SCRIPT_PATH) in calls
    assert mock_cf.domains == [
        {"environment": "production", "hostname": "api-sync-v042.example.com", "service": "cnsr-control", "zone_id": ZONE_ID}
    ]
    assert json.loads(parts_by_name(mock_cf)["metadata"].body)["bindings"][0]["namespace_id"] == namespace_id


def test_ensure_control_plane_refuses_a_different_zone_without_changing_anything(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    existing = json.dumps({"host": "api-sync-v042.example.net", "zone": "example.net", "client_token": "ct", "admin_token": "at"})
    mock_cf.kv[namespace_id]["meta:control"] = existing

    with pytest.raises(CnsrError) as info:
        control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert str(info.value) == (
        "控制端已经部署在 example.net 下，与当前设置的域名 example.com 不一致。"
        "请把本机配置里的 CLOUDFLARE_ZONE 改回 example.net；"
        "如果确实要换域名，先在 Cloudflare 里删除名为 cnsr-control 的 KV 命名空间和 Worker，再重新运行。"
    )
    calls = mock_cf.calls()
    assert ("PUT", SCRIPT_PATH) not in calls and ("PUT", DOMAINS_PATH) not in calls
    assert not mock_cf.scripts and not mock_cf.domains
    assert not [c for c in calls if c[0] in ("PUT", "POST", "DELETE")]
    assert mock_cf.kv[namespace_id]["meta:control"] == existing
    assert len(mock_cf.namespaces) == 1


@pytest.mark.parametrize("stored_zone", ["EXAMPLE.com", "example.com.", "Example.Com."])
def test_ensure_control_plane_compares_zones_ignoring_case_and_trailing_dot(mock_cf, stored_zone):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    existing = {"host": "api-sync-v042.example.com", "zone": stored_zone, "client_token": "ct", "admin_token": "at"}
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps(existing)

    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert meta.zone == stored_zone and meta.host == "api-sync-v042.example.com"
    assert ("PUT", SCRIPT_PATH) in mock_cf.calls() and ("PUT", DOMAINS_PATH) in mock_cf.calls()


def test_ensure_control_plane_ignores_namespaces_with_other_titles(mock_cf):
    mock_cf.add_namespace("something-else")
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert sorted(mock_cf.namespaces.values()) == ["cnsr-control", "something-else"]


def test_ensure_control_plane_finds_the_namespace_on_a_later_page(mock_cf):
    for index in range(mock_cf.namespaces_per_page):
        mock_cf.add_namespace(f"other-{index}")
    namespace_id = mock_cf.add_namespace("cnsr-control")
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert ("POST", NAMESPACES_PATH) not in mock_cf.calls()
    assert json.loads(parts_by_name(mock_cf)["metadata"].body)["bindings"][0]["namespace_id"] == namespace_id
    assert namespace_id in mock_cf.kv and "meta:control" in mock_cf.kv[namespace_id]


def test_ensure_control_plane_rerun_updates_the_script_and_keeps_everything_else(mock_cf):
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    first = stored_meta(mock_cf)
    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(9))
    assert stored_meta(mock_cf) == first
    assert mock_cf.calls().count(("PUT", SCRIPT_PATH)) == 2
    assert mock_cf.calls().count(("POST", NAMESPACES_PATH)) == 1
    assert [d["hostname"] for d in mock_cf.domains] == [first["host"]] * 2


def test_ensure_control_plane_unknown_zone_raises_before_changing_anything(mock_cf):
    with pytest.raises(CnsrError):
        control.ensure_control_plane(mock_cf.client, "other.example.org", random.Random(1))
    assert not mock_cf.namespaces and not mock_cf.scripts and not mock_cf.domains


# --- workers.dev address for the nodes ---


def test_ensure_control_plane_serves_the_worker_on_workers_dev_for_the_nodes(mock_cf):
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert mock_cf.workers_dev == {"cnsr-control": {"enabled": True, "previews_enabled": False}}
    assert meta.node_host == "cnsr-control.acct-sub.workers.dev"
    assert stored_meta(mock_cf)["node_host"] == "cnsr-control.acct-sub.workers.dev"


def test_ensure_control_plane_sets_up_a_workers_dev_subdomain_when_the_account_has_none(mock_cf):
    mock_cf.workers_subdomain = None
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert re.fullmatch(r"cnsr-[0-9a-f]{8}", mock_cf.workers_subdomain)
    assert meta.node_host == f"cnsr-control.{mock_cf.workers_subdomain}.workers.dev"


def test_ensure_control_plane_records_the_node_host_of_an_existing_control_plane(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    existing = {"host": "api-sync-v042.example.com", "zone": "example.com", "client_token": "ct", "admin_token": "at"}
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps(existing)

    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert meta.node_host == "cnsr-control.acct-sub.workers.dev"
    assert stored_meta(mock_cf) == {**existing, "node_host": "cnsr-control.acct-sub.workers.dev"}


def test_ensure_control_plane_leaves_an_up_to_date_meta_alone(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    existing = {
        "host": "api-sync-v042.example.com", "zone": "example.com", "client_token": "ct", "admin_token": "at",
        "node_host": "cnsr-control.acct-sub.workers.dev",
    }  # fmt: skip
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps(existing)

    control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))

    assert not [c for c in mock_cf.calls() if c[0] == "PUT" and "/values/" in c[1]]


def test_ensure_control_plane_reports_a_failed_workers_dev_setup(mock_cf):
    mock_cf.deny("POST", f"{SCRIPT_PATH}/subdomain", 500)
    with pytest.raises(CloudflareError):
        control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))


@pytest.mark.parametrize("status", [200, 400, 404])
def test_a_missing_account_subdomain_is_told_by_its_error_code(mock_cf, status):
    mock_cf.workers_subdomain, mock_cf.no_subdomain_status = None, status
    _, meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert meta.node_host == f"cnsr-control.{mock_cf.workers_subdomain}.workers.dev"


def test_another_subdomain_lookup_failure_is_reported(mock_cf):
    mock_cf.deny("GET", f"/accounts/{ACCOUNT}/workers/subdomain", 403)
    with pytest.raises(CloudflareError):
        control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    assert ("PUT", f"/accounts/{ACCOUNT}/workers/subdomain") not in mock_cf.calls()


def test_cloudflare_errors_keep_their_codes():
    error = CloudflareError(400, ["no subdomain"], [10007])
    assert error.codes == [10007] and CloudflareError(500, ["x"]).codes == []
