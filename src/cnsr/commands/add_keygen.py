"""A key pair cnsr up makes with ssh-keygen when no agent key fits, kept in ``~/.ssh/cnsr/<alias>``.

ssh-keygen asks whether to set a passphrase. Without one, ssh reads the private key straight
from the file; with one, the key goes to the session's ssh-agent once (ssh-add asks for the
passphrase), since cnsr runs ssh without a terminal to ask on.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cnsr.deploy.context import Deps
from cnsr.errors import CnsrError
from cnsr.paths import private_dir
from cnsr.ssh.agent import SESSION_AGENT, AgentKey


def made_key_path(deps: Deps, name: str) -> Path:
    return deps.key_dir / name


def offer_label(deps: Deps, alias: str) -> str:
    return f"生成一把新钥匙（{made_key_path(deps, alias)}）"


def use_made(deps: Deps, path: Path) -> tuple[str | None, AgentKey]:
    """The agent to name and the key, for a key pair cnsr made before."""
    try:
        line = path.with_name(path.name + ".pub").read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise CnsrError(f"读不到 {path}.pub：{exc}。请恢复这个文件，或删除 {path} 后重跑以生成新钥匙。") from None
    key = AgentKey(line, path.name)
    if not deps.key_has_passphrase(path):
        return None, key
    agent = _system_agent()
    _hand_to_agent(deps, path, key, agent)
    return agent, key


def make(deps: Deps, alias: str) -> tuple[str | None, AgentKey]:
    """Make a key pair for ``alias`` with ssh-keygen, then use it as ``use_made`` does."""
    path = made_key_path(deps, alias)
    if path.exists():  # made before: ssh-keygen would only ask whether to overwrite it
        return use_made(deps, path)
    private_dir(path.parent)
    deps.ui.info(
        f"接下来 ssh-keygen 会生成 {path}，并问你要不要设密码：可以设，也可以直接回车不设。"
        "设了密码，cnsr 会再用 ssh-add 把钥匙交给系统的 ssh-agent，密码只在那时输入一次。"
    )
    with deps.ui.handover():  # ssh-keygen asks on the terminal itself
        deps.generate_key(path, alias)
    agent, key = use_made(deps, path)
    deps.ui.info(f"请把私钥 {path} 备份到安全的地方（例如密码管理器）；丢了它，就只能通过服务器厂商的控制台登录这台节点。")
    return agent, key


def _system_agent() -> str | None:
    """The system's ssh-agent as cnsr names it: Windows has one named pipe (None), others SSH_AUTH_SOCK."""
    return None if sys.platform == "win32" else SESSION_AGENT


def _hand_to_agent(deps: Deps, path: Path, key: AgentKey, agent: str | None) -> None:
    """Make sure the system's agent holds the key with a passphrase."""
    if agent not in deps.detect_agents():
        missing = "Windows 的 OpenSSH Authentication Agent 服务没有运行" if agent is None else "环境变量 SSH_AUTH_SOCK 没有设置"
        raise CnsrError(
            f"钥匙 {path} 设了密码，需要系统的 ssh-agent 替 cnsr 拿着它，但没有找到正在运行的 ssh-agent"
            f"（{missing}）。可以启动 ssh-agent 后重跑；或者删除 {path} 和 {path}.pub，重跑时不设密码。"
        )
    held = [held.line.split()[:2] for held in deps.list_keys(agent)]
    if key.line.split()[:2] in held:
        return
    with deps.ui.handover():  # ssh-add asks for the passphrase on the terminal itself
        deps.add_key_to_agent(path)
