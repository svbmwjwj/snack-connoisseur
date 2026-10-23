"""Finding an SSH agent, listing its keys and keeping one public key file per server.

cnsr never touches private keys: whatever agent holds them (Bitwarden, 1Password or
the system ssh-agent) does the signing.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cnsr.errors import CnsrError
from cnsr.ssh.tools import program

_KEY_TYPE = re.compile(r"(ssh|ecdsa|sk)-\S+")
# Stands for "the agent that SSH_AUTH_SOCK names in the current session". Its socket path can
# change with every login, so this name is what gets stored; OpenSSH reads
# ``IdentityAgent SSH_AUTH_SOCK`` the same way.
SESSION_AGENT = "SSH_AUTH_SOCK"
# An agent that asks you to confirm or unlock may take a while; one that never answers must not hang cnsr.
SSH_ADD_TIMEOUT = 30


@dataclass
class AgentKey:
    """One identity offered by an agent: the public key line and its comment."""

    line: str
    comment: str


def detect_agents(
    env: Mapping[str, str],
    home: Path,
    platform: str,
    exists: Callable[[str], bool] = os.path.exists,
) -> list[str | None]:
    """Return the usable agent sockets in order of preference.

    ``None`` stands for the default agent, which on Windows is the OpenSSH named pipe.
    Elsewhere only sockets that exist are kept, without duplicates.
    """
    if platform == "win32":
        return [None]
    paths = known_sockets(home, platform)
    if env.get("SSH_AUTH_SOCK"):
        paths.append(env["SSH_AUTH_SOCK"])
    found: list[str | None] = []
    for path in paths:
        if path not in found and exists(path):
            found.append(path)
    return found


def known_sockets(home: Path, platform: str) -> list[str]:
    """The fixed sockets of the agents cnsr looks for, Bitwarden first; none on Windows."""
    if platform == "win32":
        return []
    if platform == "darwin":
        candidates = [
            home / "Library" / "Containers" / "com.bitwarden.desktop" / "Data" / ".bitwarden-ssh-agent.sock",
            home / ".bitwarden-ssh-agent.sock",  # Bitwarden from its website rather than the App Store
            home / "Library" / "Group Containers" / "2BUA8C4S2C.com.1password" / "t" / "agent.sock",
        ]
    else:
        candidates = [home / ".bitwarden-ssh-agent.sock", home / ".1password" / "agent.sock"]
    return [str(path) for path in candidates]


def agent_socket(agent: str | None, env: Mapping[str, str]) -> str | None:
    """The socket behind ``agent``; ``SESSION_AGENT`` means the current value of SSH_AUTH_SOCK."""
    if agent == SESSION_AGENT:
        return env.get("SSH_AUTH_SOCK") or None
    return agent


def list_keys(agent: str | None, run=subprocess.run) -> list[AgentKey]:
    """List the identities of ``agent`` with ``ssh-add -L``.

    An agent without identities (exit code 1) yields an empty list. One that does not answer
    within ``SSH_ADD_TIMEOUT`` seconds is an error.
    """
    label = agent if agent is not None else "系统默认 agent"
    env = dict(os.environ)
    if agent is not None:
        env["SSH_AUTH_SOCK"] = agent
    try:
        result = run(
            [program("ssh-add"), "-L"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=SSH_ADD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise CnsrError(
            f"SSH agent（{label}）在 {SSH_ADD_TIMEOUT} 秒内没有回应，请确认它已启动、已解锁，没有在等你确认。"
        ) from None
    except OSError as exc:
        raise CnsrError(f"无法运行 ssh-add，读取不到 SSH agent（{label}）里的钥匙，请先安装 OpenSSH 客户端。") from exc
    if result.returncode == 1:
        return []
    if result.returncode == 2:
        raise CnsrError(f"无法连接 SSH agent（{label}），请确认它已启动并已解锁。")
    if result.returncode != 0:
        raise CnsrError(f"读取 SSH agent（{label}）里的钥匙失败（ssh-add 退出码 {result.returncode}）。")
    return _parse_keys(result.stdout)


def find_key(keys: list[AgentKey], name: str) -> AgentKey | None:
    """Return the first key whose comment is exactly ``name``."""
    for key in keys:
        if key.comment == name:
            return key
    return None


def ensure_pub_file(key: AgentKey, name: str, pub_dir: Path) -> Path:
    """Return ``<pub_dir>/<name>.pub``, writing it from ``key`` when it is missing.

    An existing file is left as it is.
    """
    if not name or name in (".", "..") or any(char in name for char in "/\\\0"):
        raise CnsrError(f"公钥文件名 {name!r} 不合法。")
    path = pub_dir / f"{name}.pub"
    if path.exists():
        return path
    pub_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"{key.line}\n".encode())
    return path


def _parse_keys(output: str) -> list[AgentKey]:
    keys: list[AgentKey] = []
    for raw in output.splitlines():
        line = raw.strip()
        fields = line.split(None, 2)
        if len(fields) < 2 or not _KEY_TYPE.fullmatch(fields[0]):
            continue
        keys.append(AgentKey(line, fields[2] if len(fields) == 3 else ""))
    return keys
