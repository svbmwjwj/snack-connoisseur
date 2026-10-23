"""One TCP connection from this computer, to tell a blocked address from a closed port."""

from __future__ import annotations

import socket
from typing import Literal

TIMEOUT_SECONDS = 8.0

Reach = Literal["open", "refused", "timeout", "error"]


def connect(host: str, port: int, timeout: float = TIMEOUT_SECONDS) -> Reach:
    """``refused`` still means the address answers; only ``timeout`` means nothing came back."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "timeout"
    except OSError:
        return "error"
