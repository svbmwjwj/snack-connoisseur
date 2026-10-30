"""Shared pytest fixtures for the cnsr tests."""

from __future__ import annotations

import json
import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from deploy_fakes import DeployFakes
from fakes import FakeDnsZone
from hetzner_harness import Api
from lightsail_harness import Aws
from tls_server import Certificate, TlsServer, default_certificate

from cnsr.cloudflare.api import CloudflareClient
from cnsr.ssh import config as ssh_config


class FakeKv:
    """In-memory stand-in for ``KvStore``."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.lists = 0  # list_keys calls so far

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def list_keys(self, prefix: str) -> list[str]:
        self.lists += 1
        return sorted(key for key in self.data if key.startswith(prefix))

    def put(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


@dataclass
class Part:
    """One part of a multipart/form-data request body."""

    name: str
    filename: str | None
    content_type: str | None
    body: bytes


def parse_multipart(request: httpx.Request) -> list[Part]:
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    parts = []
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        headers = head.decode()
        name = re.search(r'name="([^"]*)"', headers)
        filename = re.search(r'filename="([^"]*)"', headers)
        content_type = re.search(r"(?im)^content-type: *(.+)$", headers)
        parts.append(
            Part(
                name=name.group(1) if name else "",
                filename=filename.group(1) if filename else None,
                content_type=content_type.group(1).strip() if content_type else None,
                body=body.removesuffix(b"\r\n"),
            )
        )
    return parts


def envelope(result, **extra) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "result": result, **extra})


class FakeCloudflare:
    """A stateful fake of the Cloudflare API, served through ``httpx.MockTransport``.

    It answers only the endpoints cnsr uses and fails loudly on any other request.
    ``deny`` makes one endpoint answer with an error status until ``allow`` lifts it.
    Use ``client`` as the ``CloudflareClient``.
    """

    zone_name = "example.com"
    zone_id = "zone-1"
    account_id = "acct-1"
    namespaces_per_page = 100
    keys_per_page = 1000

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.namespaces: dict[str, str] = {}  # namespace id -> title
        self.kv: dict[str, dict[str, str]] = {}  # namespace id -> {key: value}
        self.scripts: dict[str, list[Part]] = {}  # script name -> uploaded parts
        self.domains: list[dict] = []  # bodies of PUT /workers/domains, in order
        self.dns_names: set[str] = set()  # names in the zone that already have DNS records
        self.workers_subdomain: str | None = "acct-sub"  # the account's <name>.workers.dev; None: not set up
        self.no_subdomain_status = 404  # how GET .../workers/subdomain answers without one (code 10007)
        self.workers_dev: dict[str, dict] = {}  # script name -> body of its last POST .../subdomain
        self._denied: dict[tuple[str, str], int] = {}
        self.client = CloudflareClient("t-secret", transport=httpx.MockTransport(self._handle))

    def deny(self, method: str, path: str, status: int = 403) -> None:
        self._denied[(method, path)] = status

    def allow(self, method: str, path: str) -> None:
        self._denied.pop((method, path), None)

    def add_namespace(self, title: str) -> str:
        namespace_id = f"ns-{len(self.namespaces) + 1}"
        self.namespaces[namespace_id] = title
        self.kv[namespace_id] = {}
        return namespace_id

    def calls(self) -> list[tuple[str, str]]:
        """(method, path below /client/v4) of every request so far."""
        return [(r.method, _api_path(r)) for r in self.requests]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, _api_path(request)
        status = self._denied.get((method, path))
        if status is not None:
            return httpx.Response(
                status, json={"success": False, "errors": [{"message": "denied"}], "result": None}
            )
        account = f"/accounts/{self.account_id}"
        params = request.url.params
        if (method, path) == ("GET", "/zones"):
            zone = {"id": self.zone_id, "account": {"id": self.account_id}}
            return envelope([zone] if params["name"] == self.zone_name else [])
        if (method, path) == ("GET", f"/zones/{self.zone_id}/dns_records"):
            name = params.get("name")
            return envelope([{"id": f"dns-{name}", "name": name, "type": "CNAME"}] if name in self.dns_names else [])
        if (method, path) == ("GET", f"{account}/workers/scripts"):
            return envelope([{"id": name} for name in self.scripts])
        if (method, path) == ("GET", f"{account}/workers/domains"):
            return envelope(self.domains)
        if (method, path) == ("PUT", f"{account}/workers/domains"):
            self.domains.append(json.loads(request.content))
            return envelope(self.domains[-1])
        if path == f"{account}/workers/subdomain":
            if method == "PUT":
                self.workers_subdomain = json.loads(request.content)["subdomain"]
            elif self.workers_subdomain is None:
                return httpx.Response(
                    self.no_subdomain_status,
                    json={"success": False, "errors": [{"code": 10007, "message": "no subdomain"}], "result": None},
                )
            return envelope({"subdomain": self.workers_subdomain})
        enable = re.fullmatch(rf"{account}/workers/scripts/([^/]+)/subdomain", path)
        if method == "POST" and enable:
            assert enable.group(1) in self.scripts, "workers.dev enabled before the script was uploaded"
            assert self.workers_subdomain is not None, "workers.dev enabled before the account had a subdomain"
            self.workers_dev[enable.group(1)] = json.loads(request.content)
            return envelope(self.workers_dev[enable.group(1)])
        upload = re.fullmatch(rf"{account}/workers/scripts/([^/]+)", path)
        if method == "PUT" and upload:
            self.scripts[upload.group(1)] = parse_multipart(request)
            return envelope({"id": upload.group(1)})
        if (method, path) == ("GET", f"{account}/storage/kv/namespaces"):
            page, per_page = int(params.get("page", 1)), min(int(params.get("per_page", 20)), self.namespaces_per_page)
            items = [{"id": i, "title": t} for i, t in self.namespaces.items()]
            return envelope(items[(page - 1) * per_page : page * per_page])
        if (method, path) == ("POST", f"{account}/storage/kv/namespaces"):
            title = json.loads(request.content)["title"]
            return envelope({"id": self.add_namespace(title), "title": title})
        keys = re.fullmatch(rf"{account}/storage/kv/namespaces/([^/]+)/keys", path)
        if method == "GET" and keys:
            prefix = params.get("prefix", "")
            names = sorted(key for key in self.kv[keys.group(1)] if key.startswith(prefix))
            start = int(params.get("cursor") or 0)
            page = names[start : start + self.keys_per_page]
            cursor = str(start + len(page)) if start + len(page) < len(names) else ""
            return envelope([{"name": name} for name in page], result_info={"count": len(page), "cursor": cursor})
        value = re.fullmatch(rf"{account}/storage/kv/namespaces/([^/]+)/values/(.+)", path)
        if value:
            store = self.kv[value.group(1)]
            if method == "PUT":
                store[value.group(2)] = next(p.body.decode() for p in parse_multipart(request) if p.name == "value")
                return envelope(None)
            if method == "DELETE":
                store.pop(value.group(2), None)
                return envelope(None)
            if method == "GET" and value.group(2) in store:
                return httpx.Response(200, text=store[value.group(2)])
            if method == "GET":
                return httpx.Response(404, json={"success": False, "errors": [{"message": "key not found"}], "result": None})
        raise AssertionError(f"unexpected request: {method} {path}")


def _api_path(request: httpx.Request) -> str:
    return request.url.path.removeprefix("/client/v4")


@pytest.fixture(autouse=True)
def fresh_ssh_config_backups(monkeypatch):
    """Every test is a new cnsr process for the ``~/.ssh/config`` backup (taken once per process)."""
    monkeypatch.setattr(ssh_config, "_backed_up", set())


@pytest.fixture
def fake_kv() -> FakeKv:
    return FakeKv()


@pytest.fixture
def mock_cf() -> FakeCloudflare:
    return FakeCloudflare()


@pytest.fixture
def no_aws_environment(monkeypatch, tmp_path):
    """Keep the tests away from the user's AWS profile, credentials and the metadata service."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def fake_dns() -> FakeDnsZone:
    return FakeDnsZone()


@pytest.fixture
def tls_server_factory(tmp_path):
    """Start local TLS servers on 127.0.0.1; every server is shut down at teardown.

    ``start(certificate=None, *, alpn=("h2",), tls_version=TLSv1_3)`` serves the default
    probe.example.com certificate unless another one is given; ``alpn=None`` offers no ALPN.
    """
    servers: list[TlsServer] = []

    def start(
        certificate: Certificate | None = None,
        *,
        alpn: tuple[str, ...] | None = ("h2",),
        tls_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3,
    ) -> TlsServer:
        server = TlsServer(certificate or default_certificate(), tmp_path, alpn=alpn, tls_version=tls_version)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def tls_server(tls_server_factory) -> TlsServer:
    """TLS 1.3 with ALPN h2 and a self-signed certificate for probe.example.com."""
    return tls_server_factory()


@pytest.fixture
def tls_server_no_alpn(tls_server_factory) -> TlsServer:
    """Like ``tls_server`` but without ALPN."""
    return tls_server_factory(alpn=None)


@pytest.fixture
def fake_deps(tmp_path, mock_cf, fake_dns) -> Iterator[DeployFakes]:
    """Fakes for ``cnsr up``: one Cloudflare account (control plane, KV and the zone's DNS records),
    one server, Lightsail, the agent and the local network checks. See ``fakes.DeployFakes``."""

    def route(request: httpx.Request) -> httpx.Response:
        # Record lookups by name, and record writes, belong to the zone; the rest (including
        # the token check's plain listing) to the account.
        if "/dns_records" in request.url.path and (request.method != "GET" or "name" in request.url.params):
            return fake_dns._handle(request)
        return mock_cf._handle(request)

    client = CloudflareClient("t-secret", transport=httpx.MockTransport(route))
    fakes = DeployFakes(tmp_path / "home", client, mock_cf, fake_dns)
    yield fakes
    fakes.terminal.close()


@pytest.fixture
def aws():
    """A real boto3 Lightsail client with dummy credentials, answered by botocore's Stubber.

    Every response added must have been used by the end of the test.
    """
    harness = Aws()
    with harness.stubber:
        yield harness
        harness.stubber.assert_no_pending_responses()


@pytest.fixture
def hz_api() -> Api:
    """A real HetznerClient for ``de-falkenstein`` answered by a scripted router, with a fake clock."""
    return Api()
