"""Hardening a small-vendor server that cnsr reached as root.

The SSH port moves without ever leaving cnsr locked out:

1. ``harden-stage.sh`` (as root on 22) adds the user admin with the key and passwordless sudo,
   makes sshd listen on 22 *and* a random high port, and turns on ufw and fail2ban.
2. A fresh login as admin on the new port must work and run ``sudo -n true``.
3. The console password for the vendor's web console is shown once and set after you confirm
   you saved it.
4. ``harden-cut.sh`` first arms a timer that restores step 1 in 180 seconds, then closes 22 and
   switches off root, password and keyboard-interactive logins.
5. Another fresh login on the new port. From here on admin on the new port is the login to
   keep (``on_cut`` hears of it); only then is the timer stopped.
6. ``sshd -T`` must show the new port alone and those three logins off, for a login as admin
   and as root alike (``-C``, so that no ``Match`` block opens them again).

A failure before step 4 rolls everything back (``harden-revert.sh all`` as root on 22, still
open). A cut that stops before arming the timer exits ``UNCHANGED`` and needs no wait. Any
other failure in steps 4 and 5 is left to the timer, because the root login may already be
gone; cnsr waits until it has fired. A connection lost while stopping the timer leaves it
unknown whether the timer still runs; admin on the new port works either way. A failed check
in step 6 (the timer is stopped by then) restores the staged state by hand. Ctrl-C before
step 4 leaves the staged server as it is: 22 is still open and a rerun stages it again.
``resume`` finishes a hardening that a run left after step 5's login.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable

from cnsr import runlog
from cnsr.errors import CnsrError
from cnsr.server.baseline import LOGINS, connection_spec, findings, parse_sshd_t, per_login
from cnsr.ssh.remote import CommandFailed, Remote, SshError, _read_asset
from cnsr.ui import Ui

ADMIN = "admin"
# The sshd drop-in harden-stage.sh writes: a server that has it was staged by cnsr.
DROP_IN = "/etc/ssh/sshd_config.d/10-cnsr.conf"
PORT_RANGE = range(20000, 60000)
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_LENGTH = 20
SAVED = "已保存"
REVERT_PROGRAM = "/usr/local/sbin/cnsr-harden-revert"
REVERT_TIMER = "cnsr-ssh-revert.timer"
# harden-cut.sh exits with this when it stopped before arming the timer and changed nothing.
UNCHANGED = 3
# Prints the timer's state ("active", "inactive", ...) and always exits 0: systemctl is-active
# exits non-zero for every state but active, which Remote.run would take for a failure.
TIMER_STATE = ["sh", "-c", 'systemctl is-active "$1" || true', "sh", REVERT_TIMER]
_TIMER_OFF = ("inactive", "failed", "unknown")
# harden-cut.sh arms the timer for 180 seconds; waiting a little longer means it has fired.
REVERT_WAIT = 200
LOGIN_TIMEOUT = 60
_KEPT_STAGED = "已恢复为 22 端口和新端口都开的状态"
_TIMER_RESTORES = "服务器会在 3 分钟内自动恢复为 22 端口和新端口都开的状态"


def pick_ssh_port(rng, busy: set[int]) -> int:
    """A random SSH port from 20000-59999 that is not in ``busy``."""
    free = [port for port in PORT_RANGE if port not in busy]
    if not free:
        raise CnsrError("20000-59999 之间没有空闲端口可以用作 SSH 端口。")
    return rng.choice(free)


def console_password(rng) -> str:
    """A password for the vendor's web console, without look-alike characters.

    Pass a ``secrets.SystemRandom()`` outside tests.
    """
    return "".join(rng.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def stage_args(port: int, pubkey_line: str, app_ports: tuple[tuple[int, str], ...]) -> list[str]:
    """The arguments of ``harden-stage.sh``. The third is ``harden-revert.sh`` in base64,
    which the script installs as the revert program; scripts get data only as arguments. The
    fourth lists the ports the firewall opens for the server's features, as ``443/tcp 443/udp``.

    The text comes from the same reader ``Remote.run_script`` streams scripts with, so the
    installed copy is exactly what a streamed ``harden-revert.sh`` would run.
    """
    revert = base64.b64encode(_read_asset("harden-revert.sh").encode("utf-8")).decode("ascii")
    return [str(port), pubkey_line, revert, " ".join(f"{number}/{protocol}" for number, protocol in app_ports)]


class Hardener:
    """Moves SSH to admin on a random high port; see the module docstring for the steps.

    ``root`` is the root login on 22 cnsr already has; ``make_remote(user, port)`` reaches the
    same server as another user on another port. ``rng`` picks the port and the console
    password, so outside tests it must be ``secrets.SystemRandom()``.
    """

    def __init__(self, root: Remote, make_remote: Callable[[str, int], Remote], ui: Ui, rng, sleep=time.sleep) -> None:
        self._root = root
        self._make_remote = make_remote
        self._ui = ui
        self._rng = rng
        self._sleep = sleep

    def run(
        self,
        pubkey_line: str,
        busy_ports: set[int],
        *,
        app_ports: tuple[tuple[int, str], ...],
        on_cut: Callable[[str, int], None] | None = None,
    ) -> tuple[str, int]:
        """Harden the server and return the user and port to log in with from now on.

        The firewall opens the SSH ports and ``app_ports``, the ``(port, protocol)`` pairs the
        server's features listen on, and nothing else.

        ``on_cut(user, port)`` is called as soon as a fresh login after the cut worked, before
        the timer is stopped: from then on only that login is sure to reach the server.
        """
        pubkey = pubkey_line.strip()
        if not pubkey or "\n" in pubkey or "\r" in pubkey:
            raise CnsrError("要装给 admin 的公钥必须是单独的一行。")
        port = pick_ssh_port(self._rng, busy_ports)
        admin = self._make_remote(ADMIN, port)
        self._ui.detail(f"开始加固：新建用户 {ADMIN}，SSH 端口改为 {port}。确认新端口能登录之前，22 端口保持开放。")
        try:
            self._stage(admin, pubkey, port, app_ports)
        except Exception as err:
            raise self._roll_back(err) from err
        self._ui.detail("新端口登录正常，开始关闭 22 端口和 root、密码登录。")
        self._cut(admin, port, on_cut)
        self._check(admin, port)
        self._ui.detail(f"加固完成：以后用 {ADMIN} 和端口 {port} 登录。")
        return ADMIN, port

    def resume(self, port: int) -> bool:
        """Finish the hardening of a server cnsr staged earlier, reached as admin on ``port``.

        A run that stopped after the cut was proven (Ctrl-C, a lost connection) leaves the cut
        server with its revert timer maybe still pending, or the staged server once the timer
        has fired. A pending timer is stopped first (this login proves the cut does not lock
        cnsr out), and nothing is read or changed until the timer is off. When the settings are
        still not final, the cut runs again under a new timer. Returns True when it had to cut
        again.
        """
        admin = self._make_remote(ADMIN, port)
        self._stop_leftover_timer(admin)
        if not self._unfinished(admin, port):
            return False
        self._ui.detail("上次加固没有做完（22 端口或 root、密码登录还开着），现在补完。")
        self._cut(admin, port, None)
        self._check(admin, port)
        self._ui.detail(f"加固完成：以后用 {ADMIN} 和端口 {port} 登录。")
        return True

    def _stop_leftover_timer(self, admin: Remote) -> None:
        """Make sure no revert timer is pending; a timer that stays on stops the run, changing nothing.

        The timer is a transient unit: once it fired or was stopped it is gone, and
        ``systemctl stop`` then fails ("not loaded", exit 5). So the stop may fail; only the
        state afterwards counts.
        """
        try:
            admin.run(["systemctl", "stop", REVERT_TIMER], sudo=True)
        except SshError:
            raise
        except CnsrError:
            pass  # usually "not loaded": nothing to stop
        state = admin.run(TIMER_STATE, sudo=True).strip()
        if state not in _TIMER_OFF:
            pending = "它最迟 3 分钟后触发，把服务器恢复为 22 端口和新端口都开的状态；之后" if state == "active" else "请稍后"
            raise CnsrError(
                f"没能停止服务器上的自动恢复计时器 {REVERT_TIMER}（状态：{state or '未知'}），没有做任何改动。"
                f"{pending}重跑同一条命令即可补完加固。"
            )

    def _cut(self, admin: Remote, port: int, on_cut: Callable[[str, int], None] | None) -> None:
        """Steps 4 and 5: the cut under the timer, the fresh login, then the timer stops."""
        try:
            admin.run_script("harden-cut.sh", [str(port)])
            self._login(admin, port)
        except CommandFailed as err:
            if err.exit_code == UNCHANGED:  # no timer was armed and nothing changed
                raise CnsrError(f"加固没有改动服务器：{err}\n修好问题后重跑同一条命令即可。") from err
            raise self._wait_for_timer(err) from err
        except Exception as err:
            raise self._wait_for_timer(err) from err
        if on_cut is not None:
            on_cut(ADMIN, port)
        try:
            admin.run(["systemctl", "stop", REVERT_TIMER], sudo=True)
        except SshError as err:
            raise self._timer_unknown(err, port) from err
        except Exception as err:
            raise self._wait_for_timer(err) from err

    def _stage(self, admin: Remote, pubkey: str, port: int, app_ports: tuple[tuple[int, str], ...]) -> None:
        self._root.run_script("harden-stage.sh", stage_args(port, pubkey, app_ports))
        self._login(admin, port)
        password = console_password(self._rng)
        runlog.secret(password)
        # Shown only until it is saved, and never logged: the display clears it afterwards.
        notice = (
            f"{ADMIN} 的控制台密码：{password}\n"
            "只在厂商网页控制台（VNC）应急登录时使用。cnsr 不保存它，也不会再显示。"
        )
        with self._ui.transient(notice):
            self._ui.confirm_text(f"存进密码管理器后输入“{SAVED}”", SAVED)
        admin.run(["chpasswd"], input=f"{ADMIN}:{password}\n", sudo=True)

    def _login(self, admin: Remote, port: int) -> None:
        """A fresh SSH login as admin on the new port that also runs ``sudo -n true``."""
        try:
            admin.run(["true"], sudo=True, timeout=LOGIN_TIMEOUT)
        except CnsrError as err:
            raise CnsrError(f"用新端口 {port} 以 {ADMIN} 登录并执行 sudo 失败：{err}") from err

    def _roll_back(self, err: Exception) -> CnsrError:
        self._ui.warn("加固没有完成，正在撤销本次改动……")
        try:
            self._root.run_script("harden-revert.sh", ["all"])
        except Exception as revert_err:
            return CnsrError(
                f"加固失败：{err}\n自动回滚也失败了：{revert_err}\n"
                f"22 端口没有关闭：可以用 root 登录服务器后运行 {REVERT_PROGRAM} all 撤销。"
            )
        return CnsrError(f"加固失败：{err}\n已回滚：SSH、防火墙和 fail2ban 恢复为加固前的设置。")

    def _wait_for_timer(self, err: Exception) -> CnsrError:
        self._ui.warn(f"关闭 22 端口后的验证失败：{err}\n{_TIMER_RESTORES}，等待 {REVERT_WAIT} 秒……")
        self._sleep(REVERT_WAIT)
        return CnsrError(f"加固失败：{err}\n{_TIMER_RESTORES}（已等待 {REVERT_WAIT} 秒），之后重跑即可。")

    def _timer_unknown(self, err: SshError, port: int) -> CnsrError:
        """The connection broke while the timer was being stopped: it may or may not run on."""
        return CnsrError(
            f"停止自动恢复计时器时 SSH 连接断开了：{err}\n"
            "服务器可能已经停止了计时器（加固已生效），也可能没有（那样它会在 3 分钟内回到 22 端口和新端口都开的状态）。"
            f"两种情况下都可以用用户 {ADMIN}、端口 {port} 登录；重跑同一条命令会检查并补完加固。"
            f"如果用 SSH 登录不上，请在厂商网页控制台（VNC）用 {ADMIN} 和刚才保存的控制台密码登录检查。"
        )

    def _unfinished(self, admin: Remote, port: int) -> list[str]:
        """What keeps the settings sshd now uses from being final (see ``_problems``), for every login."""
        contexts = {
            user: parse_sshd_t(admin.run(["sshd", "-T", "-C", connection_spec(user)], sudo=True)) for user in LOGINS
        }
        return per_login(contexts, lambda sshd: _problems(sshd, port))

    def _check(self, admin: Remote, port: int) -> None:
        """Read the settings sshd now uses; when they are not as intended, restore the staged state."""
        try:
            problems = self._unfinished(admin, port)
            if not problems:
                return
            error = "加固后核验 SSH 设置未通过：" + "；".join(problems)
        except Exception as err:
            error = f"加固后读取 SSH 设置失败：{err}"
        # The timer is already stopped, so put the staged state back by hand.
        try:
            admin.run_script("harden-revert.sh", ["stage"])
        except Exception as revert_err:
            raise CnsrError(
                f"{error}\n恢复也失败了：{revert_err}\n现在请用 {ADMIN} 和端口 {port} 登录服务器检查 SSH 设置。"
            ) from revert_err
        raise CnsrError(f"{error}\n{_KEPT_STAGED}，检查服务器的 SSH 设置后重跑。")


def _problems(sshd: dict[str, str], port: int) -> list[str]:
    """What keeps the final settings from being: only ``port``, no root, no password logins."""
    problems = []
    if sshd.get("port") != str(port):
        problems.append(f"SSH 端口应只有 {port}，实际为 {sshd.get('port') or '未知'}")
    problems += findings(sshd)
    if sshd.get("permitrootlogin") not in ("no", "yes"):  # "yes" is among the findings already
        problems.append(f"SSH 仍允许 root 登录（permitrootlogin {sshd.get('permitrootlogin') or '未知'}）")
    return problems
