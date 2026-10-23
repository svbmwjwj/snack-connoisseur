"""Workers KV namespace access over the Cloudflare API."""

from __future__ import annotations

from urllib.parse import quote

from cnsr.cloudflare.api import CloudflareClient, CloudflareError, envelope


class KvStore:
    """String values in one KV namespace."""

    def __init__(self, cf: CloudflareClient, account_id: str, namespace_id: str):
        self._cf = cf
        self._base = f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}"

    def _value_path(self, key: str) -> str:
        return f"{self._base}/values/{quote(key, safe='')}"

    def get(self, key: str) -> str | None:
        """Return the stored value, or None when the key does not exist."""
        try:
            response = self._cf.request("GET", self._value_path(key), raw=True)
        except CloudflareError as exc:
            if exc.status == 404:
                return None
            raise
        return response.content.decode("utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        """Every key name starting with ``prefix``, following the API's cursor."""
        names: list[str] = []
        cursor = ""
        while True:
            params = {"prefix": prefix, **({"cursor": cursor} if cursor else {})}
            body = envelope(self._cf.request("GET", f"{self._base}/keys", params=params, raw=True))
            names += [entry["name"] for entry in body.get("result") or []]
            cursor = (body.get("result_info") or {}).get("cursor") or ""
            if not cursor:
                return names

    def put(self, key: str, value: str) -> None:
        self._cf.request("PUT", self._value_path(key), files={"value": (None, value), "metadata": (None, "{}")})

    def delete(self, key: str) -> None:
        self._cf.request("DELETE", self._value_path(key))
