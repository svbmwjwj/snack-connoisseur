"""The label ``cnsr=<alias>`` that marks the Hetzner resources cnsr created for a node."""

from __future__ import annotations

from typing import Any

TAG_KEY = "cnsr"


def labels(alias: str) -> dict[str, str]:
    """The labels a new resource of node ``alias`` gets."""
    return {TAG_KEY: alias}


def labelled(found: dict[str, Any], alias: str) -> bool:
    """Whether an API resource carries the label ``cnsr=<alias>``."""
    return (found.get("labels") or {}).get(TAG_KEY) == alias


def selector(alias: str) -> str:
    """The label selector that finds node ``alias``'s resources."""
    return f"{TAG_KEY}={alias}"
