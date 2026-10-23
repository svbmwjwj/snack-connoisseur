"""Test doubles for the deploy pipeline."""

from __future__ import annotations

import json
import re

import httpx

from cnsr.cloudflare.api import CloudflareClient


class FakeDnsZone:
    """A stateful fake of one zone's DNS records, served through ``httpx.MockTransport``.

    It answers only the ``dns_records`` endpoints and fails loudly on any other request.
    ``deny`` makes one kind of write (or lookup) on one record type (``"*"``: a lookup of every
    type) answer with an error status. Use ``client`` as the ``CloudflareClient``.
    """

    zone_id = "zone-1"

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.records: dict[str, dict] = {}  # record id -> record
        self._counter = 0
        self._denied: dict[tuple[str, str], int] = {}
        self.client = CloudflareClient("t-secret", transport=httpx.MockTransport(self._handle))

    def deny(self, method: str, rtype: str, status: int = 500) -> None:
        """Answer ``method`` requests about ``rtype`` records with ``status``."""
        self._denied[(method, rtype)] = status

    def allow(self, method: str, rtype: str) -> None:
        self._denied.pop((method, rtype), None)

    def calls(self) -> list[tuple[str, str]]:
        """(method, path below the zone's dns_records) of every request so far."""
        prefix = f"/client/v4/zones/{self.zone_id}/dns_records"
        return [(r.method, r.url.path.removeprefix(prefix)) for r in self.requests]

    def of_type(self, rtype: str) -> list[dict]:
        return [record for record in self.records.values() if record["type"] == rtype]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = f"/client/v4/zones/{self.zone_id}/dns_records"
        match = re.fullmatch(rf"{re.escape(base)}(?:/([^/]+))?", request.url.path)
        if match is None:
            raise AssertionError(f"unexpected request: {request.method} {request.url.path}")
        record_id, method = match.group(1), request.method
        body = json.loads(request.content) if request.content else {}
        if record_id is None and method == "GET":  # a lookup by name, of one type or of all
            rtype, name = request.url.params.get("type"), request.url.params["name"]
            denied = self._deny_response(method, rtype or "*")
            if denied is not None:
                return denied
            return _ok([r for r in self.records.values() if r["name"] == name and rtype in (None, r["type"])])
        if record_id is None and method == "POST":
            denied = self._deny_response(method, body["type"])
            if denied is not None:
                return denied
            self._counter += 1
            record = {**body, "id": f"rec-{self._counter}"}
            self.records[record["id"]] = record
            return _ok(record)
        record = self.records.get(record_id or "")
        if record is None:
            return _error(404, "record not found")
        denied = self._deny_response(method, record["type"])
        if denied is not None:
            return denied
        if method == "PATCH":
            record.update(body)
            return _ok(record)
        if method == "DELETE":
            del self.records[record["id"]]
            return _ok({"id": record["id"]})
        raise AssertionError(f"unexpected request: {method} {request.url.path}")

    def _deny_response(self, method: str, rtype: str) -> httpx.Response | None:
        status = self._denied.get((method, rtype))
        return None if status is None else _error(status, "denied")


def _ok(result) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"success": False, "errors": [{"message": message}], "result": None})
