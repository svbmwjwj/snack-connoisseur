"""The label ``cnsr=<alias>`` that marks the Hetzner resources cnsr created for a node."""

from __future__ import annotations

from typing import Any

TAG_KEY = "cnsr"


def server_name(alias: str) -> str:
    """The name of node ``alias``'s server: a hostname, so an underscore becomes a hyphen.

    Hetzner takes only letters, digits, hyphens and dots in a server's name; the other
    resources and the label keep the alias as it is.
    """
    return alias.replace("_", "-")


def labels(alias: str) -> dict[str, str]:
    """The labels a new resource of node ``alias`` gets."""
    return {TAG_KEY: alias}


def labelled(found: dict[str, Any], alias: str) -> bool:
    """Whether an API resource carries the label ``cnsr=<alias>``."""
    return (found.get("labels") or {}).get(TAG_KEY) == alias


def selector(alias: str) -> str:
    """The label selector that finds node ``alias``'s resources."""
    return f"{TAG_KEY}={alias}"
