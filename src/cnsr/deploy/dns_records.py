"""Per-node DNS records, and the move of the SSH alias from the node's IP to its domain.

The deploy reaches the node by IP address throughout. The records are created early and
never looked up before that, so no resolver caches a "not found" answer. At the very end the
alias is switched to the domain, once this machine resolves it and an SSH login through it works.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Callable
from pathlib import Path

from cnsr.cloudflare.api import CloudflareClient, CloudflareError
from cnsr.cloudflare.dns import delete_record, fresh_name, upsert_record
from cnsr.waiting import Deadline
from cnsr.ssh.config import read_block, write_block
from cnsr.state import Node

CHECK_INTERVAL_SECONDS = 10


def ensure_node_records(cf: CloudflareClient, zone_id: str, zone: str, node: Node, rng: random.Random) -> None:
    """Make the node's domain point at its addresses and remember the record ids on ``node``.

    A node without a domain gets a random one under ``zone`` that has no records yet, so a
    record that is not cnsr's is never taken over. The domain is set before any record is
    written, so a failure part way still leaves it on the node for the rerun. The A record
    always exists; the AAAA record only while the node has an IPv6 address that did not fail
    its last outside test (``publish_aaaa`` adds it once a test passes), and one left over
    from earlier is deleted.
    """
    if not node.domain:
        node.domain = fresh_name(cf, zone_id, zone, rng)
    node.dns_record_ids["A"] = upsert_record(cf, zone_id, node.domain, "A", node.ipv4, comment=record_comment(node))
    if node.ipv6 and not node.ipv6_probe_failed:
        publish_aaaa(cf, zone_id, node)
    else:
        drop_aaaa(cf, zone_id, node)


def publish_aaaa(cf: CloudflareClient, zone_id: str, node: Node) -> None:
    """Point the node's domain at its IPv6 address and remember the record's id."""
    comment = record_comment(node)
    node.dns_record_ids["AAAA"] = upsert_record(cf, zone_id, node.domain, "AAAA", node.ipv6, comment=comment)


def record_comment(node: Node) -> str:
    """The note on the node's records in the Cloudflare dashboard, naming the node they belong to."""
    return f"cnsr node {node.alias}"


def drop_aaaa(cf: CloudflareClient, zone_id: str, node: Node) -> None:
    """Delete the node's AAAA record and forget its id; nothing happens when there is none."""
    record_id = node.dns_record_ids.get("AAAA")
    if record_id is None:
        return
    delete_record(cf, zone_id, record_id)
    del node.dns_record_ids["AAAA"]


def delete_node_records(cf: CloudflareClient, zone_id: str, node: Node) -> list[str]:
    """Delete every record stored for ``node`` and return a Chinese message per failure.

    Ids of deleted records are removed from the node; the ones that failed stay, so a
    later attempt can try again.
    """
    errors: list[str] = []
    for rtype, record_id in list(node.dns_record_ids.items()):
        try:
            delete_record(cf, zone_id, record_id)
        except CloudflareError as exc:
            errors.append(f"删除 {node.domain or node.alias} 的 {rtype} 记录失败：{exc}")
        else:
            del node.dns_record_ids[rtype]
    return errors


def switch_alias_when_ready(
    node: Node,
    ssh_config: Path,
    can_login_via: Callable[[str], bool],
    resolves: Callable[[str, set[str]], bool],
    *,
    wait: int = 120,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Point the node's SSH alias at its domain once the domain works; True when it does.

    Every 10 seconds, for at most ``wait`` seconds, ``resolves`` must confirm that this
    machine resolves the domain to the node (its IPv4 address, and no address that is not
    the node's: a local resolver may filter AAAA), and then ``can_login_via`` must
    confirm that an SSH login through the domain works. Only then is the alias's HostName
    rewritten; its other settings stay as they are. On a timeout nothing is written and the
    alias keeps the IP. An alias that already uses the domain counts as switched.

    False without waiting means there is nothing to switch: no domain, no alias, or an
    alias block with no HostName.
    """
    block = read_block(ssh_config, node.alias)
    if block is None or not block.hostname or not node.domain:
        return False
    if block.hostname == node.domain:
        return True
    expected = {node.ipv4} | ({node.ipv6} if node.ipv6 else set())
    deadline = Deadline(wait, clock)
    while True:
        if resolves(node.domain, expected) and can_login_via(node.domain):
            write_block(ssh_config, dataclasses.replace(block, hostname=node.domain))
            return True
        if deadline.passed():
            return False
        sleep(deadline.step(CHECK_INTERVAL_SECONDS))
