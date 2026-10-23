"""Names generated or checked by cnsr: the control host label and node aliases."""

from __future__ import annotations

import random
import re

LABEL_WORDS = ("edge", "cache", "static", "media", "assets", "cdn", "api", "sync", "files", "metrics", "gateway", "data")

_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def random_label(rng: random.Random) -> str:
    """Return a bland-looking host label such as ``edge-cache-v123``.

    The two words always differ.
    """
    first, second = rng.sample(LABEL_WORDS, 2)
    return f"{first}-{second}-v{rng.randrange(1000):03d}"


def validate_alias(alias: str) -> str | None:
    """Return None when ``alias`` is a usable node alias, else a Chinese reason."""
    if _ALIAS.fullmatch(alias):
        return None
    return "别名只能包含小写字母、数字、连字符和下划线，必须以字母或数字开头，最长 32 个字符。"
