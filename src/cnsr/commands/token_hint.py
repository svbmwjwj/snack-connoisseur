"""What to do when Cloudflare stops accepting the API token saved in ``secrets.env``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cnsr.cloudflare.api import CloudflareError
from cnsr.errors import CnsrError


@contextmanager
def saved_token_hint(conf_path: Path) -> Iterator[None]:
    """Turn Cloudflare refusing the token (HTTP 401 or 403) into how to replace it.

    The token is only asked for when ``secrets.env`` has none, so the way out of a token that
    was revoked, expired or lost a permission is to delete it from there and run again.
    """
    try:
        yield
    except CloudflareError as exc:
        if exc.status not in (401, 403):
            raise
        raise CnsrError(
            f"Cloudflare 拒绝了保存的 API token（HTTP {exc.status}），它可能已被撤销、过期或缺少权限。"
            f"请打开 {conf_path}，删除 CLOUDFLARE_API_TOKEN 那一行后重跑同一条命令，按提示输入新的 token。"
        ) from exc
