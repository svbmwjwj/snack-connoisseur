"""Test doubles for ``Hardener``: one fake server seen through the root login and the admin logins.

``FakeServer`` names every step in ``log``; ``FakeRemote`` is the ``Remote`` for one user and port.
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable
from dataclasses import dataclass

from cnsr.errors import CnsrError
from cnsr.server.harden import TIMER_STATE, Hardener
from cnsr.ssh.remote import SshError
from cnsr.ui import Ui

SCRIPT_STEPS = {"harden-stage.sh": "stage", "harden-cut.sh": "cut"}
DISARM = ["systemctl", "stop", "cnsr-ssh-revert.timer"]
# What Remote.run raises for `systemctl stop` of the transient timer once it fired or was stopped.
NOT_LOADED = "远程命令执行失败（退出码 5）：\nFailed to stop cnsr-ssh-revert.timer: Unit cnsr-ssh-revert.timer not loaded."
STOP_DENIED = "远程命令执行失败（退出码 1）：\nFailed to stop cnsr-ssh-revert.timer: Access denied"
# The final check reads sshd's settings for a login as admin and as root.
ADMIN_CHECK = "sshd -T -C user=admin,host=cnsr,addr=192.0.2.1"
ROOT_CHECK = "sshd -T -C user=root,host=cnsr,addr=192.0.2.1"
CHECKS = [ADMIN_CHECK, ROOT_CHECK]


@dataclass
class Call:
    step: str
    user: str
    port: int
    sudo: bool
    detail: object = None


class FakeRemote:
    def __init__(self, server: FakeServer, user: str, port: int) -> None:
        self.server, self.user, self.port = server, user, port

    def run_script(self, name, args, *, sudo=True):
        assert name in (*SCRIPT_STEPS, "harden-revert.sh"), name
        self.server.step(SCRIPT_STEPS.get(name) or "revert " + " ".join(args), self, sudo, list(args))
        if name == "harden-cut.sh":
            self.server.timer = "active"  # armed before anything else changes
        return ""

    def run(self, command, *, input=None, sudo=False, timeout=600):
        if command == ["true"]:
            self.server.step(f"login {self.user}:{self.port}", self, sudo, timeout)
            if not (self.server.login_ok.pop(0) if self.server.login_ok else True):
                raise SshError("port")
            return ""
        if command == ["chpasswd"]:
            self.server.step("chpasswd", self, sudo, input)
            return ""
        if command == DISARM:
            self.server.step("disarm", self, sudo)
            return self.server.stop_timer()
        if command == TIMER_STATE:
            self.server.step("timer state", self, sudo)
            return "active\n" if self.server.timer == "active" else "inactive\n"
        if command[:2] == ["sshd", "-T"]:
            self.server.step(" ".join(command), self, sudo)
            return self.server.sshd_t(self.port, *command[2:])
        raise AssertionError(f"unexpected command {command}")


class FakeServer:
    """``login_ok`` answers successive admin logins (True once it runs out); a step named in
    ``failing`` raises ``CnsrError``, one named in ``errors`` raises that exception; ``answers``
    are typed at the prompt (an exception is raised instead). ``sshd_t(port, *options)`` is what
    ``sshd -T`` prints. ``timer`` is the revert timer, a transient unit: "active" once the cut
    armed it, "unloaded" after it fired or was stopped; ``timer_stop_fails`` keeps it active."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.calls: list[Call] = []
        self.login_ok: list[bool] = []
        self.failing: set[str] = set()
        self.errors: dict[str, BaseException] = {}
        self.sshd_t: Callable[..., str] = lambda port, *options: sshd_t([port])
        self.answers: list = ["已保存"]
        self.shown_before_prompt: list[str] = []
        self.slept: list[float] = []
        self.made: list[tuple[str, int]] = []
        self.timer = "unloaded"
        self.timer_stop_fails = False
        self.out = io.StringIO()
        self.ui = Ui(input_fn=self._type, out=self.out, interactive=True)
        self.root = FakeRemote(self, "root", 22)

    def make_remote(self, user: str, port: int) -> FakeRemote:
        self.made.append((user, port))
        return FakeRemote(self, user, port)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def step(self, name: str, remote: FakeRemote, sudo: bool, detail: object = None) -> None:
        self.log.append(name)
        self.calls.append(Call(name, remote.user, remote.port, sudo, detail))
        if name in self.errors:
            raise self.errors[name]
        if name in self.failing:
            raise CnsrError(f"{name} 出错")

    def stop_timer(self) -> str:
        """``systemctl stop`` of the revert timer, as it ends on a server."""
        if self.timer_stop_fails:
            raise CnsrError(STOP_DENIED)
        if self.timer == "unloaded":
            raise CnsrError(NOT_LOADED)
        self.timer = "unloaded"
        return ""

    def call(self, step: str) -> Call:
        return next(call for call in self.calls if call.step == step)

    @property
    def password(self) -> str:
        return self.call("chpasswd").detail.removeprefix("admin:").removesuffix("\n")

    def _type(self, prompt: str) -> str:
        answer = self.answers.pop(0) if self.answers else "已保存"
        if isinstance(answer, BaseException):
            raise answer
        self.shown_before_prompt.append(self.out.getvalue())
        self.log.append(f"confirm {answer}")
        return answer


def sshd_t(ports: list[int], **settings: str) -> str:
    values = {"permitrootlogin": "no", "passwordauthentication": "no", "kbdinteractiveauthentication": "no", **settings}
    return "".join(f"port {port}\n" for port in ports) + "".join(f"{key} {value}\n" for key, value in values.items())


def hardener(fakes: FakeServer, seed: int = 1) -> Hardener:
    return Hardener(fakes.root, fakes.make_remote, fakes.ui, random.Random(seed), sleep=fakes.sleep)


def port_of(fakes: FakeServer) -> int:
    return fakes.made[0][1]


def checks(fakes: FakeServer) -> list[str]:
    """The ``sshd -T`` reads in ``log``."""
    return [step for step in fakes.log if step.startswith("sshd -T")]
