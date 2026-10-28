"""Pipeline step 8: the node's SNI rotation service (cnsr-rotate.sh under systemd).

It runs last, once the node is registered with its line and token: the service asks the
control plane for work as soon as it starts, and stops for good when its token is refused.

An installed service is stopped before a run of cnsr up changes the node (``stop_rotation``, called first
in step 5), so it cannot rotate while cnsr rewrites backups.json and config.json or runs
verify-reality.sh. Its TERM handler only undoes a switch it had not finished; one that even
that could not undo is settled here before the service starts again (``_settle_switch``).
line-pending.json is left alone: it holds the line config.json already runs, which the service
writes to the control plane first thing.
"""

from __future__ import annotations

from cnsr.deploy.context import DeployContext
from cnsr.errors import CnsrError
from cnsr.server.xray import NODE_DIR
from cnsr.ssh.remote import _read_asset
from cnsr.waiting import Deadline

BIN_DIR = f"{NODE_DIR}/bin"
SERVICE = "cnsr-rotate.service"
UNIT_PATH = f"/etc/systemd/system/{SERVICE}"
# The service runs cnsr-rotate.sh, which sources the two rotate-*.sh files and calls the others.
SCRIPTS = (
    "cnsr-rotate.sh", "rotate-control.sh", "rotate-switch.sh",
    "validate-candidates.sh", "verify-reality.sh", "china-check.sh",
)  # fmt: skip
# Exists from just before a changed file of an installed service is replaced (its scripts, its
# unit, or node.env and identity.env, which it reads only when it starts) until it has restarted.
RESTART_MARKER = f"{NODE_DIR}/.rotate-restart-pending"
# Left by the service: stopped after its token was refused, rotate-failed holds back retries.
# Every add removes both, so a node that is added again polls again at once.
# unauthorized-since starts a spell of refused tokens, last-version is the version of the last
# line the service wrote: neither may outlive the run (a node deployed again starts over at version 1).
HOLD_MARKERS = (
    f"{NODE_DIR}/stopped",
    f"{NODE_DIR}/rotate-failed",
    f"{NODE_DIR}/unauthorized-since",
    f"{NODE_DIR}/last-version",
)
# A switch in progress (its pending line, then its backup entry), and a switch that is done but
# not yet written to the control plane (cnsr-rotate.sh).
SWITCH_MARK = f"{NODE_DIR}/switching"
PENDING_LINE = f"{NODE_DIR}/line-pending.json"
# A service that fails at once (a bad env file, say) is no longer active after this long.
SETTLE_SECONDS = 3
# The node's own look at the control plane (cnsr-rotate.sh check): a new node's record, or a
# workers.dev subdomain set up moments ago, can take a few minutes to reach the edge it talks to.
CHECK_SECONDS = 300
CHECK_INTERVAL_SECONDS = 5
_CHECK_TEXT = {
    "000": "没有收到回应",
    "401": "控制端不认节点 token，HTTP 401",
    "404": "控制端没有这个接口，HTTP 404",
}
_IS_ACTIVE = 'systemctl is-active "$1" || true'


def put_service_input(ctx: DeployContext, path: str, content: str) -> None:
    """Write a file the service reads at startup (mode 600), owing it a restart when it changed.

    A file that is new is simply written: there is no service yet that read an older one.
    """
    current = ctx.read(path)
    if current == content:
        return
    remote = ctx.remote()
    if current is not None:
        remote.run(["touch", RESTART_MARKER], sudo=True)
    remote.put_text(path, content, "600")


def stop_rotation(ctx: DeployContext) -> None:
    """Stop the service when it is installed; ``step_rotate`` starts it again."""
    if not ctx.exists(UNIT_PATH):
        return
    ctx.deps.ui.detail("先停下换 SNI 服务，部署完成后再启动。")
    ctx.rotation_stopped = True  # first: an interrupt right after the stop must still restart it
    ctx.remote().run(["systemctl", "stop", SERVICE], sudo=True)


def resume_rotation(ctx: DeployContext) -> None:
    """Start the service again after a run that stopped it and then failed or was interrupted.

    Best effort: the run's own error is the one to report, so a start that fails only warns.
    """
    if not ctx.rotation_stopped:
        return
    try:
        ctx.remote().run(["systemctl", "start", SERVICE], sudo=True)
    except BaseException as exc:  # a second Ctrl-C too: say so, then let it go on up
        reason = "已中断" if isinstance(exc, KeyboardInterrupt) else str(exc)
        ctx.deps.ui.warn(
            f"换 SNI 服务没能重新启动（{reason}），这台节点暂时不会自动换 SNI；"
            f"修好问题后重跑 cnsr up {ctx.node.alias} 即可恢复。"
        )
        if not isinstance(exc, Exception):
            raise
        return
    ctx.rotation_stopped = False
    ctx.deps.ui.detail("部署没有完成，换 SNI 服务已重新启动。")


def _settle_switch(ctx: DeployContext) -> None:
    """Clear a switch the service left unfinished, before it starts and would undo it.

    Undoing it would put config.prev.json back over the config this add has just verified.
    When that config is the switch's, the switch is recorded as done, so the service writes
    the line to the control plane; otherwise the switch never took effect and is dropped.
    """
    mark = ctx.read(SWITCH_MARK)
    if mark is None:
        return
    xray, remote = ctx.node.xray, ctx.remote()
    line = f'{{"sni":"{xray.sni}","dest_ip":"{xray.dest_ip}"}}'  # pending_line in cnsr-rotate.sh
    if mark.split("\n", 1)[0] == line and ctx.read(PENDING_LINE) is None:
        remote.put_text(PENDING_LINE, line + "\n", "600")
    remote.run(["rm", "-f", SWITCH_MARK], sudo=True)


def step_rotate(ctx: DeployContext) -> None:
    """Install the service's files, (re)start it, check that it keeps running and that the node
    reaches the control plane.

    Only files whose content differs are written. A service that was installed before is
    restarted when one of them changed, or when this run or an earlier one replaced one of
    them or an env file (``put_service_input``) without restarting it yet (``RESTART_MARKER``).
    """
    ui = ctx.deps.ui
    remote = ctx.remote()
    wanted = {f"{BIN_DIR}/{name}": (_read_asset(name), "755") for name in SCRIPTS}
    wanted[UNIT_PATH] = (_read_asset(SERVICE), "644")
    restart = ctx.exists(RESTART_MARKER)
    current = {path: ctx.read(path) for path in wanted}
    changed = [path for path, (content, _) in wanted.items() if current[path] != content]
    if any(current[path] is not None for path in changed):
        remote.run(["touch", RESTART_MARKER], sudo=True)
        restart = True
    remote.run(["install", "-d", "-m", "755", BIN_DIR], sudo=True)
    for path in changed:
        content, mode = wanted[path]
        remote.put_text(path, content, mode)
    ui.detail(f"换 SNI 服务的文件：{'更新了 ' + '、'.join(changed) if changed else '都已是最新'}。")
    remote.run(["systemctl", "daemon-reload"], sudo=True)
    _settle_switch(ctx)
    remote.run(["rm", "-f", *HOLD_MARKERS], sudo=True)
    remote.run(["systemctl", "enable", "--now", SERVICE], sudo=True)
    if restart:
        ui.detail("重启换 SNI 服务，让它用上新文件。")
        remote.run(["systemctl", "restart", SERVICE], sudo=True)
        remote.run(["rm", "-f", RESTART_MARKER], sudo=True)
    ctx.deps.sleep(SETTLE_SECONDS)
    state = _service_state(ctx)
    if state != "active":
        raise CnsrError(
            f"换 SNI 服务没有在节点上运行起来（状态：{state}）。可以登录节点用 "
            f"journalctl -u cnsr-rotate 查看原因，修好后重跑同一条命令即可。"
        )
    ui.detail("换 SNI 服务正在运行。")
    _check_control(ctx)


def _check_control(ctx: DeployContext) -> None:
    """Have the node ask the control plane for its task until it is answered.

    A 403 fails at once: the Worker never answers it, so it is Cloudflare's own challenge or block.
    """
    ui, host = ctx.deps.ui, ctx.meta.node_host
    ui.detail(f"让节点自己连一次控制端 {host}。")
    command = [f"{BIN_DIR}/cnsr-rotate.sh", "check"]
    deadline = Deadline(CHECK_SECONDS, ctx.deps.clock)
    first = True
    while first or not deadline.passed():
        if not first:
            ctx.deps.sleep(deadline.step(CHECK_INTERVAL_SECONDS))
        status = ctx.remote().run(command, sudo=True).strip()
        if status == "200":
            ui.detail("节点能连上控制端。")
            if not first:
                _restart_after_wait(ctx)
            return
        if status == "403":
            raise CnsrError(
                f"节点连控制端 {host} 被 Cloudflare 拦下了（HTTP 403），这台节点收不到换 SNI 的任务。"
                f"请在 Cloudflare 后台“安全性 → 事件”里按主机名 {host} 查看是哪项设置拦的，关掉后重跑同一条命令。"
            )
        first = False
    reason = _CHECK_TEXT.get(status, f"HTTP {status}")
    raise CnsrError(
        f"等了 {CHECK_SECONDS // 60} 分钟，节点连不上控制端 {host}（{reason}），这台节点收不到换 SNI 的任务。"
        f"可以登录节点用 journalctl -u cnsr-rotate 查看，修好后重跑同一条命令。"
    )


def _restart_after_wait(ctx: DeployContext) -> None:
    """Start the service afresh: while the node waited for the control plane it may have taken
    the refused token for a removed node and stopped for good."""
    remote = ctx.remote()
    ctx.deps.ui.detail("节点等了一会儿才连上控制端，重启换 SNI 服务。")
    remote.run(["rm", "-f", *HOLD_MARKERS], sudo=True)
    remote.run(["systemctl", "restart", SERVICE], sudo=True)
    ctx.deps.sleep(SETTLE_SECONDS)
    state = _service_state(ctx)
    if state != "active":
        raise CnsrError(f"换 SNI 服务重启后没有运行起来（状态：{state}）。可以登录节点用 journalctl -u cnsr-rotate 查看原因。")


def _service_state(ctx: DeployContext) -> str:
    """What ``systemctl is-active`` prints for the service (it exits non-zero unless active)."""
    out = ctx.remote().run(["sh", "-c", _IS_ACTIVE, "sh", SERVICE], sudo=True)
    return out.strip() or "未知"
