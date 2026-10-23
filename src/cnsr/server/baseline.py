"""Reading `sshd -T` and checking the SSH login settings every node must have.

``baseline-check.sh`` prints `sshd -T` on a Lightsail server and the hardening reads it again
after closing port 22. ``findings`` lists, in Chinese, every login setting that is too open.
A ``Match`` block can open a setting for some logins only, so the settings are read for a
login as each of ``LOGINS`` (``sshd -T -C``) and ``per_login`` checks all of them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

# The logins whose settings are checked: cnsr's own user, and root.
LOGINS = ("admin", "root")
# baseline-check.sh prints "cnsr-context <user>" before the settings for that login.
CONTEXT_MARK = "cnsr-context"

# Settings that must be exactly "no", and what to say when they are not (or are missing).
_MUST_BE_NO = (
    ("passwordauthentication", "SSH 允许密码登录"),
    ("kbdinteractiveauthentication", "SSH 允许键盘交互登录"),
)
_ROOT_PASSWORD = "SSH 允许 root 用密码登录"


def parse_sshd_t(text: str) -> dict[str, str]:
    """Turn `sshd -T` output into ``{setting: value}`` with lower-case setting names.

    A setting printed on several lines (``port``, ``listenaddress``, ...) maps to its values
    in order, joined by single spaces.
    """
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            values.setdefault(key.lower(), []).append(value.strip())
    return {key: " ".join(found) for key, found in values.items()}


def findings(sshd: dict[str, str]) -> list[str]:
    """Every login setting in ``sshd`` that is too open; an empty list means it is fine.

    Password and keyboard-interactive logins must be off. Root may log in with a key
    (``prohibit-password``), never with a password (``yes``).
    """
    found = [message for key, message in _MUST_BE_NO if sshd.get(key) != "no"]
    if sshd.get("permitrootlogin") == "yes":
        found.append(_ROOT_PASSWORD)
    return found


def connection_spec(user: str) -> str:
    """The ``sshd -T -C`` argument for a login as ``user`` (from a documentation address)."""
    return f"user={user},host=cnsr,addr=192.0.2.1"


def parse_contexts(text: str) -> dict[str, dict[str, str]]:
    """Split ``baseline-check.sh`` output into the ``sshd -T`` settings of each login."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == CONTEXT_MARK:
            current = sections.setdefault(words[1], [])
        elif current is not None:
            current.append(line)
    return {user: parse_sshd_t("\n".join(lines)) for user, lines in sections.items()}


def per_login(
    contexts: Mapping[str, dict[str, str]], check: Callable[[dict[str, str]], list[str]] = findings
) -> list[str]:
    """``check`` for the settings of every login in ``LOGINS``, each problem once.

    A problem that only some logins have names them. A login whose settings are missing is
    checked as empty, so it fails closed.
    """
    found = {user: check(contexts.get(user, {})) for user in LOGINS}
    problems: list[str] = []
    for user in LOGINS:
        for problem in found[user]:
            users = [other for other in LOGINS if problem in found[other]]
            text = problem if len(users) == len(LOGINS) else f"{problem}（{'、'.join(users)} 登录时）"
            if text not in problems:
                problems.append(text)
    return problems
