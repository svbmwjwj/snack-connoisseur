"""Node inventory records and the control-plane metadata kept in KV."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Literal, TypeVar

from cnsr import runlog
from cnsr.cloudflare.kv import KvStore
from cnsr.errors import CnsrError

META_KEY = "meta:control"
NODE_PREFIX = "node:"
# The SNI rotation state of one node, owned by the control plane's Worker.
STATE_PREFIX = "state:"
# A JSON array of every node's alias. The Worker reads it instead of listing the namespace, which
# the free plan allows only 1,000 times a day. It may name a node whose record is gone, never
# the other way round.
INDEX_KEY = "index:nodes"

T = TypeVar("T")


@dataclass
class SshInfo:
    user: str
    port: int
    key_name: str


@dataclass
class XrayInfo:
    uuid: str
    public_key: str
    short_id: str
    sni: str
    dest_ip: str

    def __post_init__(self) -> None:
        runlog.secret(self.uuid)


@dataclass
class Node:
    """One proxy node. The control-plane Worker reads ``alias`` and ``line`` from the JSON form."""

    alias: str
    source: Literal["aws", "vps"]
    ipv4: str
    ipv6: str | None = None
    region: str | None = None
    instance_name: str | None = None
    static_ip_name: str | None = None
    # The static IP was the user's before (cnsr add --static-ip): cnsr rm detaches it and keeps it.
    static_ip_reused: bool = False
    # The instance that static IP was moved from, given it back when the first deployment fails.
    static_ip_from: str | None = None
    domain: str | None = None
    dns_record_ids: dict[str, str] = field(default_factory=dict)
    ssh: SshInfo | None = None
    xray: XrayInfo | None = None
    node_token: str | None = field(default=None, repr=False)
    line: str | None = None
    # The last outside test of the node's IPv6 failed: its AAAA record waits for one that passes.
    ipv6_probe_failed: bool = False

    def __post_init__(self) -> None:
        runlog.secret(self.node_token)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Node:
        """Parse a stored record. Unknown keys are ignored; a bad record raises ``ValueError``."""
        data = _object(json.loads(raw))
        for key, nested in (("ssh", SshInfo), ("xray", XrayInfo)):
            if data.get(key) is not None:
                data[key] = _build(nested, data[key])
        return _build(cls, data)


@dataclass
class ControlMeta:
    """The control plane's address and tokens, stored under ``meta:control``.

    ``host`` is the address in the user's zone that clients use; ``node_host`` is the Worker's
    workers.dev address, which the nodes use (see ``control.ensure_control_plane``).
    """

    host: str
    zone: str
    client_token: str = field(repr=False)
    admin_token: str = field(repr=False)
    node_host: str = ""

    def __post_init__(self) -> None:
        runlog.secret(self.client_token)
        runlog.secret(self.admin_token)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> ControlMeta:
        return _build(cls, _object(json.loads(raw)))


class Inventory:
    """Nodes and control metadata kept as JSON values in the control plane's KV namespace."""

    def __init__(self, kv: KvStore):
        self._kv = kv

    def get(self, alias: str) -> Node | None:
        return self._read(NODE_PREFIX + alias, Node.from_json)

    def put(self, node: Node) -> None:
        """Store the record; its alias goes into the index first, so the index never misses it."""
        self.keep_indexed(node.alias)
        self._kv.put(NODE_PREFIX + node.alias, node.to_json())

    def aliases(self) -> list[str]:
        """Every node's alias, sorted. Writes nothing; a missing index costs one KV list."""
        return sorted(self._index()[0])

    def keep_indexed(self, alias: str) -> None:
        """Add the alias to the index unless it is there already.

        ``cnsr add`` calls this once more at the end: another ``add`` running at the same time
        may have rewritten the index without this node.
        """
        aliases, stored = self._index()
        if alias not in aliases or not stored:
            self._write_index(aliases | {alias})

    def delete(self, alias: str) -> None:
        """Delete the record, then take its alias out of the index."""
        self._kv.delete(NODE_PREFIX + alias)
        aliases, stored = self._index()
        if alias in aliases or not stored:
            self._write_index(aliases - {alias})

    def delete_state(self, alias: str) -> None:
        """Delete the node's SNI rotation state (``state:<alias>``)."""
        self._kv.delete(STATE_PREFIX + alias)

    def clear_exhausted(self, alias: str) -> bool:
        """Mark the node's backups as no longer used up; True when the state had to change.

        Only ``exhausted`` changes. A state that is missing or not a JSON object is left
        alone: the Worker reads it as a fresh state, which is not exhausted either.
        """
        raw = self._kv.get(STATE_PREFIX + alias)
        try:
            state = json.loads(raw) if raw is not None else None
        except ValueError:
            return False
        if not isinstance(state, dict) or state.get("exhausted") is not True:
            return False
        state["exhausted"] = False
        self._kv.put(STATE_PREFIX + alias, json.dumps(state, ensure_ascii=False))
        return True

    def meta(self) -> ControlMeta | None:
        return self._read(META_KEY, ControlMeta.from_json)

    def put_meta(self, meta: ControlMeta) -> None:
        self._kv.put(META_KEY, meta.to_json())

    def _index(self) -> tuple[set[str], bool]:
        """The aliases in the index, and whether a usable index was stored.

        A control plane from before the index, or one whose index is damaged, has its node
        records listed instead: the index written from them then keeps every existing node.
        """
        raw = self._kv.get(INDEX_KEY)
        try:
            aliases = json.loads(raw) if raw is not None else None
        except ValueError:
            aliases = None
        if isinstance(aliases, list) and all(isinstance(alias, str) for alias in aliases):
            return set(aliases), True
        return {key.removeprefix(NODE_PREFIX) for key in self._kv.list_keys(NODE_PREFIX)}, False

    def _write_index(self, aliases: set[str]) -> None:
        self._kv.put(INDEX_KEY, json.dumps(sorted(aliases), ensure_ascii=False))

    def _read(self, key: str, parse: Callable[[str], T]) -> T | None:
        raw = self._kv.get(key)
        if raw is None:
            return None
        try:
            return parse(raw)
        except ValueError as exc:
            raise CnsrError(f"控制端里的记录 {key} 已损坏，无法读取。") from exc


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("record is not a JSON object")
    return dict(value)


def _build(cls: type[T], value: Any) -> T:
    """Build ``cls`` from a mapping, dropping keys it does not know."""
    data = _object(value)
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    try:
        return cls(**{key: item for key, item in data.items() if key in known})
    except TypeError as exc:
        raise ValueError(f"invalid {cls.__name__} record: {exc}") from exc
