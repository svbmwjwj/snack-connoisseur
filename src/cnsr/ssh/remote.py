"""Running commands on a server through the system OpenSSH client.

Every server operation goes through ``Remote``. Each call starts one ``ssh`` process with
an argument list (never a shell on this machine), ``ControlMaster=no`` and
``ControlPath=none``, so it never rides on a connection the user's own ssh keeps open. Everything ssh
needs to know about the server is passed as ``-o`` options next to ``-F <ssh_config>``, so
the call does not depend on what the user's config says.

Server-side scripts are streamed to ``bash -s -- <args>`` on stdin and must never read
stdin themselves; their data travels as arguments. Data for a single command such as
``chpasswd`` goes through ``Remote.run(..., input=...)``. Input is sent as bytes and output
is decoded here, because text mode would turn LF into CRLF on Windows.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from cnsr.errors import CnsrError
from cnsr.ssh.tools import program

Kind = Literal["port", "key", "password", "hostkey", "other"]

logger = logging.getLogger(__name__)

_ASSET_PACKAGE = "cnsr.assets.node"
_HINTS: dict[Kind, str] = {
    "port": "连不上 SSH 端口：检查 IP、端口和厂商防火墙",
    "key": "密钥被拒：确认公钥已装到服务器、agent 已解锁",
    "password": "密码错误：cnsr 不会自动重试，确认后重跑",
    "hostkey": "主机指纹不符：如果刚重装过系统，可以删除旧指纹后重试",
    "other": "SSH 连接失败。",
}
# Checked in this order. A wrong password is reported by ssh as "please try again" and then
# as a final "(publickey,password)" line, so the password marker must come before the key one.
_MARKERS: tuple[tuple[Kind, tuple[str, ...]], ...] = (
    ("hostkey", ("remote host identification has changed", "host key verification failed")),
    (
        "port",
        ("connection refused", "timed out", "no route to host", "network is unreachable", "could not resolve hostname"),
    ),
    ("password", ("permission denied, please try again", "permission denied (password")),
    ("key", ("permission denied (publickey",)),
)
_NOISE = "Warning: Permanently added"
_TAIL_LINES = 5
_LOG_TAIL_LINES = 10
_MODE = re.compile(r"[0-7]{3,4}")
# Runs on the server under ``sh -c`` (so the login shell need not be POSIX) and reads the key
# line from stdin. It adds a newline first when authorized_keys does not end with one, so the
# new key never joins the last line, and it skips a line that is already there, so a rerun
# adds nothing. One command line, no shell function, nothing beyond POSIX utilities.
INSTALL_KEY_SCRIPT = " ".join(
    [
        "umask 077;",
        "mkdir -p ~/.ssh || exit 1;",
        "f=~/.ssh/authorized_keys;",
        'touch "$f" || exit 1;',
        "IFS= read -r key || exit 1;",
        'grep -qxF -- "$key" "$f" && exit 0;',
        r"""if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then printf '\n' >> "$f" || exit 1; fi;""",
        r'''printf '%s\n' "$key" >> "$f"''',
    ]
)


@dataclass
class Target:
    """A server as ssh sees it; mirrors the fields of ``HostBlock``."""

    alias: str
    hostname: str
    user: str
    port: int
    identity_file: str | None
    identity_agent: str | None


class SshError(CnsrError):
    """ssh itself failed (exit status 255); ``kind`` says why, the message says what to do.

    ``unreachable``: nothing came back from the server's address (a timeout before TCP was up, or
    no route). A refusal or a timeout during the banner exchange did reach the server.
    """

    def __init__(self, kind: Kind, message: str | None = None, *, unreachable: bool | None = None) -> None:
        super().__init__(message or _HINTS[kind])
        self.kind = kind
        self.unreachable = kind == "port" if unreachable is None else unreachable


class CommandFailed(CnsrError):
    """A remote command ran and exited with ``exit_code`` (neither 0 nor ssh's own 255)."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def classify(stderr: str) -> Kind:
    """Name the cause of a failed ssh connection from what ssh printed."""
    text = stderr.lower()
    for kind, markers in _MARKERS:
        if any(marker in text for marker in markers):
            return kind
    return "other"


# Two minutes for a command; the bundled scripts that install packages first wait up to two
# minutes for apt's lock on a fresh server, and the Docker install and image pulls download.
COMMAND_TIMEOUT = 120
SCRIPT_TIMEOUTS = {
    "preflight.sh": 150,  # up to about seven probes of 20 seconds each
    "baseline-check.sh": 420,
    "harden-stage.sh": 420,
    "harden-cut.sh": 120,
    "harden-revert.sh": 120,
    "install-docker.sh": 600,
    "validate-candidates.sh": 420,
    "verify-reality.sh": 300,
}


# A script gets this many seconds less on the server than here, so the server ends it first.
SERVER_MARGIN = 15
# timeout(1) exits 124 when TERM stopped the command, 137 when KILL had to.
_STOPPED_BY_TIMEOUT = (124, 137)


class Remote:
    """Runs commands, scripts and file writes on one server."""

    def __init__(self, target: Target, ssh_config: Path, run: Callable = subprocess.run) -> None:
        self.target = target
        self.ssh_config = ssh_config
        self._run = run

    def run(
        self,
        command: list[str],
        *,
        input: str | None = None,
        sudo: bool = False,
        timeout: int = COMMAND_TIMEOUT,
        label: str | None = None,
        server_limit: bool = False,
    ) -> str:
        """Run ``command`` on the server and return its stdout.

        ``sudo`` adds ``sudo -n`` unless the login user is root. Exit status 255 means ssh
        failed and raises ``SshError``; any other failure raises ``CnsrError``. A command still
        running after ``timeout`` seconds is stopped; the error names it by ``label`` or itself.
        ``true`` cannot hang, so a timeout of that login check is an ``SshError``.

        ``server_limit``: the server stops the command itself (``timeout``) ``SERVER_MARGIN``
        seconds before this machine would, since killing ssh leaves it running there.
        """
        label = label or f"远程命令 {shlex.join(command)}"
        login_check = command == ["true"]
        if server_limit:
            command = ["timeout", "-k", "10", str(max(timeout - SERVER_MARGIN, 1)), *command]
        if sudo and self.target.user != "root":
            command = ["sudo", "-n", *command]
        settings = [("BatchMode", "yes"), *_connection(self.target), *_identity(self.target)]
        args = _ssh_args(self.target, self.ssh_config, settings, shlex.join(command))
        stdin = {"input": input.encode("utf-8")} if input is not None else {"stdin": subprocess.DEVNULL}
        began = time.monotonic()
        try:
            done = _spawn(self._run, args, capture_output=True, timeout=timeout, **stdin)
        except subprocess.TimeoutExpired:
            took = time.monotonic() - began
            logger.info("[%s] %s → 超过 %s 秒未完成，已中止，耗时 %.1f 秒", self.target.alias, shlex.join(command), timeout, took)
            if login_check:
                raise SshError("other", f"SSH 登录超过 {timeout} 秒仍未完成，已中止。", unreachable=False) from None
            raise CnsrError(f"{label} 超过 {timeout} 秒仍未完成，已中止。") from None
        stdout, stderr = _decode(done.stdout), _decode(done.stderr)
        self._log_run(command, done.returncode, time.monotonic() - began, stdout, stderr)
        if done.returncode == 255:
            raise _connection_error(stderr)
        if server_limit and done.returncode in _STOPPED_BY_TIMEOUT:
            raise CnsrError(f"{label} 超过 {timeout} 秒仍未完成，已在服务器上中止。")
        if done.returncode != 0:
            tail = _tail(stderr) or _tail(stdout)  # some tools (xray run -test) say why on stdout
            raise CommandFailed(done.returncode, f"远程命令执行失败（退出码 {done.returncode}）" + (f"：\n{tail}" if tail else "。"))
        return stdout

    def run_script(self, name: str, args: list[str], *, sudo: bool = True) -> str:
        """Stream the bundled server script ``name`` to ``bash -s -- <args>`` on stdin."""
        logger.info("[%s] 运行服务器脚本 %s %s", self.target.alias, name, shlex.join(args))
        timeout = SCRIPT_TIMEOUTS.get(name, COMMAND_TIMEOUT)
        command = ["bash", "-s", "--", *args]
        return self.run(
            command, input=_read_asset(name), sudo=sudo, timeout=timeout, label=f"服务器脚本 {name}", server_limit=True
        )

    def put_text(self, path: str, content: str, mode: str = "600") -> None:
        """Write ``content`` to ``path`` as root with ``mode``; the parent directory must exist.

        The bytes travel on stdin of a single ``install`` command, never through a script, so
        the file holds exactly ``content``. The file is created with ``mode`` from the start.
        """
        if not _MODE.fullmatch(mode):
            raise CnsrError(f"文件权限 {mode!r} 不是八进制数字（例如 600）。")
        if not path or path.startswith("-"):
            raise CnsrError(f"服务器上的文件路径不合法：{path!r}")
        logger.info("[%s] 写入文件 %s（权限 %s）", self.target.alias, path, mode)
        self.run(["install", "-m", mode, "/dev/stdin", path], input=content, sudo=True)

    def _log_run(self, command: list[str], code: int, took: float, stdout: str, stderr: str) -> None:
        """One log line per command: where, what, the exit code and how long. Output only when it failed."""
        target = self.target
        where = f"[{target.alias}] {target.user}@{target.hostname}:{target.port}"
        line = f"{where} 执行 {shlex.join(command)} → 退出码 {code}，耗时 {took:.1f} 秒"
        if code != 0:
            for label, text in (("stdout", stdout), ("stderr", stderr)):
                tail = _tail(text, _LOG_TAIL_LINES)
                if tail:
                    line += f"\n{label} 末尾：\n{tail}"
        logger.info("%s", line)

    def can_login(self) -> bool:
        """True when a trivial command runs; any ssh failure gives False, other errors propagate."""
        try:
            self.run(["true"], timeout=60)
        except SshError:
            return False
        return True


def install_key_with_password(
    target: Target, pubkey_line: str, ssh_config: Path, run: Callable = subprocess.run
) -> None:
    """Add ``pubkey_line`` to the server's authorized_keys, logging in with a password.

    The key goes on a line of its own and is not added a second time (``INSTALL_KEY_SCRIPT``).
    OpenSSH asks for the password on the terminal and reads it from there, not from stdin,
    which carries only the key line. Nothing is captured, so the prompt and ssh's own
    messages stay visible.
    """
    line = pubkey_line.strip()
    if not line or "\n" in line or "\r" in line:
        raise CnsrError("要安装的公钥必须是单独的一行。")
    settings = [
        ("BatchMode", "no"),
        *_connection(target),
        ("PubkeyAuthentication", "no"),
        ("PreferredAuthentications", "password,keyboard-interactive"),
    ]
    args = _ssh_args(target, ssh_config, settings, shlex.join(["sh", "-c", INSTALL_KEY_SCRIPT]))
    done = _spawn(run, args, input=(line + "\n").encode("utf-8"))
    if done.returncode == 255:
        raise SshError("other", "SSH 连接或密码登录失败（退出码 255），原因见上方 ssh 的输出。")
    if done.returncode != 0:
        raise CnsrError(f"用密码安装公钥失败（退出码 {done.returncode}），原因见上方 ssh 的输出。")


def forget_host_key(hostname: str, port: int, run: Callable = subprocess.run) -> None:
    """Delete the server's entry from known_hosts.

    ssh-keygen exits 0 whether or not the host was listed. A known_hosts file that does not
    exist (exit 255, "Cannot stat ...: No such file or directory") has nothing to forget.
    Any other failure, such as a file that cannot be rewritten, raises ``CnsrError``.
    """
    name = hostname if port == 22 else f"[{hostname}]:{port}"
    done = _spawn(run, [program("ssh-keygen"), "-R", name], capture_output=True)
    if done.returncode == 0:
        return
    output = _decode(done.stderr) + "\n" + _decode(done.stdout)
    if "cannot stat" in output.lower() and "no such file" in output.lower():
        return
    tail = _tail(output)
    raise CnsrError(f"删除旧主机指纹失败（退出码 {done.returncode}），known_hosts 可能无法写入" + (f"：\n{tail}" if tail else "。"))


def _connection(target: Target) -> list[tuple[str, str]]:
    return [
        ("ConnectTimeout", "10"),
        # A connection that went dead mid-command is given up within a minute.
        ("ServerAliveInterval", "15"),
        ("ServerAliveCountMax", "4"),
        ("StrictHostKeyChecking", "accept-new"),
        ("ControlMaster", "no"),
        ("ControlPath", "none"),
        ("HostName", target.hostname),
        ("Port", str(target.port)),
        ("User", target.user),
    ]


def _identity(target: Target) -> list[tuple[str, str]]:
    settings: list[tuple[str, str]] = []
    if target.identity_file:
        settings += [("IdentitiesOnly", "yes"), ("IdentityFile", target.identity_file)]
    if target.identity_agent:
        settings.append(("IdentityAgent", target.identity_agent))
    return settings


def _ssh_args(target: Target, ssh_config: Path, settings: Sequence[tuple[str, str]], command: str) -> list[str]:
    args = [program("ssh"), "-F", str(ssh_config)]
    for name, value in settings:
        args += ["-o", f"{name}={_option_value(name, value)}"]
    # "--" keeps an alias that starts with "-" from being read as an option.
    return [*args, "--", target.alias, command]


def _option_value(name: str, value: str) -> str:
    """Quote ``value`` the way ssh reads option values: double quotes when it has spaces."""
    if any(ord(char) < 32 or ord(char) == 127 or char == '"' for char in value):
        raise CnsrError(f"SSH 选项 {name} 的值含有不能传给 ssh 的字符。")
    if any(char.isspace() or char == "'" for char in value):
        if value.endswith("\\"):
            raise CnsrError(f"SSH 选项 {name} 的值含有不能传给 ssh 的字符。")
        return f'"{value}"'
    return value


def _spawn(run: Callable, args: list[str], **kwargs):
    try:
        return run(args, **kwargs)
    except FileNotFoundError:
        raise CnsrError(f"找不到 {args[0]} 命令，请先安装 OpenSSH 客户端。") from None
    except OSError:
        raise CnsrError(f"无法运行 {args[0]}，请检查 OpenSSH 客户端是否可用。") from None


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")


def _tail(stderr: str, count: int = _TAIL_LINES) -> str:
    lines = [line.rstrip() for line in stderr.splitlines()]
    return "\n".join([line for line in lines if line and not line.startswith(_NOISE)][-count:])


def _connection_error(stderr: str) -> SshError:
    kind = classify(stderr)
    text = stderr.lower()
    unreachable = kind == "port" and (
        ("timed out" in text and "banner exchange" not in text)
        or "network is unreachable" in text
        or "no route to host" in text
    )
    if kind != "other":
        return SshError(kind, unreachable=unreachable)
    tail = _tail(stderr)
    return SshError(kind, "SSH 连接失败（退出码 255）" + (f"：\n{tail}" if tail else "。"))


def _read_asset(name: str) -> str:
    """The text of a bundled server script, with LF line endings and no BOM."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise CnsrError(f"服务器脚本名称不合法：{name!r}")
    try:
        data = (resources.files(_ASSET_PACKAGE) / name).read_bytes()
    except (ModuleNotFoundError, FileNotFoundError, IsADirectoryError, NotADirectoryError):
        raise CnsrError(f"找不到服务器脚本 {name}，安装包可能不完整。") from None
    return data.decode("utf-8-sig").replace("\r\n", "\n")
