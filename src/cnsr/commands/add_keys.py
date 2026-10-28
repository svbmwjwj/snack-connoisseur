"""The SSH agent and key ``cnsr up`` deploys with. cnsr only ever handles public keys.

Without a fitting agent key, ssh-keygen can make one (``add_keygen``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from cnsr import localconf
from cnsr.commands import add_keygen
from cnsr.deploy.context import Deps
from cnsr.errors import CnsrError
from cnsr.paths import private_dir
from cnsr.ssh.agent import SESSION_AGENT, AgentKey, ensure_pub_file, find_key

# Also a valid Lightsail key pair name, since the key is imported under it.
_KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def choose_login(deps: Deps, alias: str, name: str) -> tuple[str | None, AgentKey, str]:
    """The agent ssh is to use (None: none named), the node's key and the key's name.

    A key pair cnsr made for ``name`` before is used as it is. Otherwise the key comes from an
    agent, or ssh-keygen makes a new one when you choose so.
    """
    made = add_keygen.made_key_path(deps, name)
    if made.exists():
        return (*add_keygen.use_made(deps, made), name)
    found = choose_agent(deps, alias)
    picked = choose_key(deps, found[1], alias, name) if found is not None else None
    if found is None or picked is None:
        return (*add_keygen.make(deps, alias), alias)
    return found[0], *picked


def choose_agent(deps: Deps, alias: str) -> tuple[str | None, list[AgentKey]] | None:
    """The agent to use and the keys it holds; None when you choose to make a new key instead.

    A remembered choice (``CNSR_SSH_AGENT``) that still holds keys wins. Otherwise agents
    without keys are left out; when several remain you pick one once, when none does you make
    a new key or name the agent's socket, and either agent answer is remembered. An agent
    found in place of a remembered one that stopped working is remembered instead of it.
    """
    ui = deps.ui
    stored = localconf.load(deps.conf_path).get("CNSR_SSH_AGENT")
    if stored and (keys := _keys(deps, stored)):
        return stored, keys
    found = [(agent, keys) for agent in deps.detect_agents() if (keys := _keys(deps, agent))]
    if len(found) == 1:
        agent, keys = found[0]
        remember = bool(stored)
    elif found:
        labels = [_agent_label(agent) for agent, _ in found]
        agent, keys = found[ui.choose("找到多个带钥匙的 SSH agent，选择 cnsr 使用哪一个（会记住这次的选择）", labels)]
        remember = True
    else:
        ui.info("没有找到带钥匙的 SSH agent（例如 Bitwarden、1Password 或系统自带的 ssh-agent）。")
        options = [add_keygen.offer_label(deps, alias), "使用其他 SSH agent（输入它的 socket 路径）"]
        if ui.choose("这台节点用哪把钥匙登录", options) == 0:
            return None
        agent = os.path.expanduser(ui.ask("SSH agent 的 socket 路径"))
        keys = _keys(deps, agent)
        if not keys:
            raise CnsrError(f"SSH agent {agent} 里没有钥匙。请先在 agent 里添加一把以节点别名命名的钥匙，再重跑。")
        remember = True
    if remember and agent is not None and agent != stored:
        localconf.save(deps.conf_path, {**localconf.load(deps.conf_path), "CNSR_SSH_AGENT": agent})
    return agent, keys


def choose_key(deps: Deps, keys: list[AgentKey], alias: str, name: str) -> tuple[AgentKey, str] | None:
    """The key named ``name`` (by comment, or by the key in ``<name>.pub``) and its name.

    Without one you pick any key of the agent, or choose to make a new one (None); a picked
    key keeps its comment as its name when that is usable as a file and key pair name, else
    it takes the alias.
    """
    key = find_key(keys, name) or _key_in_file(keys, deps.pub_dir / f"{name}.pub")
    if key is not None:
        return key, name
    deps.ui.info(
        f"agent 里没有名为 {name} 的钥匙（按公钥备注匹配）。建议在 agent 里新建一把名为 {alias} 的钥匙，"
        "一台服务器一把；这次也可以先从下面选一把，或让 cnsr 用 ssh-keygen 生成一把新的。"
    )
    labels = [f"{k.comment or '（没有备注）'}（{k.line.split()[0]}）" for k in keys]
    picked = deps.ui.choose("选择这台节点使用的钥匙", [*labels, add_keygen.offer_label(deps, alias)])
    if picked == len(keys):
        return None
    chosen = keys[picked]
    return chosen, chosen.comment if _KEY_NAME.fullmatch(chosen.comment) else alias


def pub_file(deps: Deps, key: AgentKey, name: str) -> Path:
    """``<pub_dir>/<name>.pub`` holding ``key``; written from the agent when it is missing.

    ``~/.ssh`` is created private first. A file that holds another key stops the run, since
    ssh would offer only that other key (``IdentitiesOnly``).
    """
    private_dir(deps.pub_dir.parent)
    path = deps.pub_dir / f"{name}.pub"
    if path.exists() and _key_in_file([key], path) is None:
        raise CnsrError(f"{path} 里的公钥和 agent 里的钥匙 {name} 不一致。请删除或改正这个文件后重跑。")
    return ensure_pub_file(key, name, deps.pub_dir)


def _agent_label(agent: str | None) -> str:
    if agent is None:
        return "系统默认 agent"
    if agent == SESSION_AGENT:
        return "环境变量 SSH_AUTH_SOCK 指向的 agent（例如系统自带的 ssh-agent）"
    return agent


def _keys(deps: Deps, agent: str | None) -> list[AgentKey]:
    try:
        return deps.list_keys(agent)
    except CnsrError as exc:  # an agent that is not running or still locked
        deps.ui.warn(str(exc))
        return []


def _key_in_file(keys: list[AgentKey], path: Path) -> AgentKey | None:
    """The key among ``keys`` that the public key file ``path`` holds (comments aside)."""
    try:
        wanted = path.read_bytes().decode("utf-8-sig").split()[:2]
    except (OSError, UnicodeDecodeError):
        return None
    if len(wanted) < 2:
        return None
    return next((key for key in keys if key.line.split()[:2] == wanted), None)
