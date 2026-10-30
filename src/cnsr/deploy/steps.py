"""The ``cnsr up`` pipeline: eight steps, each of which looks at the real state before acting.

Steps 1-3 prepare and harden the server; steps 4-8 deploy the Xray node and run only when
that feature is chosen (see ``steps_for``).

A run that stops part way (an error, Ctrl-C) is continued by running the same command again.
So a step cut off by a dropped connection runs once more, once the server answers again.
"""

from __future__ import annotations

from cnsr.aws.lightsail import REGION_NAMES
from cnsr.cloudflare.api import CloudflareError
from cnsr import features
from cnsr.deploy.context import AddRequest, DeployContext
from cnsr.deploy.steps_host import step_connect, step_harden, step_ip
from cnsr.deploy.steps_node import step_register, step_xray
from cnsr.deploy.steps_rotate import resume_rotation, step_rotate
from cnsr.deploy.steps_sni import step_scan, step_sni
from cnsr.hetzner.cloud import REGION_NAMES as HETZNER_REGION_NAMES
from cnsr.ssh.remote import SshError
from cnsr.waiting import Deadline

HOST_STEPS = (step_ip, step_connect, step_harden)
XRAY_STEPS = (step_scan, step_sni, step_xray, step_register, step_rotate)
STEPS = HOST_STEPS + XRAY_STEPS
TITLES = {
    "step_ip": "确定服务器地址",
    "step_connect": "连接服务器并建解析",
    "step_harden": "加固",
    "step_scan": "扫描候选 SNI",
    "step_sni": "在节点上验证并选定 SNI",
    "step_xray": "部署 Xray",
    "step_register": "登记节点并切换 SSH 别名",
    "step_rotate": "启动换 SNI 服务",
}

# Steps 1 and 2 are not run again: they make the first login, with waits and a diagnosis of their own.
RERUNNABLE_FROM = 3
NETWORK_WAIT_SECONDS = 120
NETWORK_RETRY_SECONDS = 10
LOGIN_TIMEOUT = 30
# A record write that got no answer is tried again after this long.
SAVE_RETRY_SECONDS = 3

__all__ = ["REGION_NAMES", "STEPS", "TITLES", "heading", "run_pipeline", "steps_for", *TITLES]


def heading(req: AddRequest) -> str:
    """The title line of the stage list: the command, the alias and where the node runs."""
    if req.source == "aws":
        return f"cnsr up {req.alias} · AWS {REGION_NAMES.get(req.region, req.region)}"
    if req.source == "hetzner":
        return f"cnsr up {req.alias} · Hetzner {HETZNER_REGION_NAMES.get(req.region, req.region)}"
    return f"cnsr up {req.alias} · 已有服务器 {req.ip}"


def steps_for(chosen: list[str]) -> tuple:
    """The steps that deploy ``chosen``: the server's own, then those of each extension."""
    return STEPS if features.XRAY in chosen else STEPS[: len(HOST_STEPS)]


def run_pipeline(ctx: DeployContext) -> None:
    """Run the steps of the chosen features in order.

    The node record is stored after each step that changed it, and also when a step fails or
    is interrupted part way, so a rerun of ``up`` (or ``rm``) finds what was already created.
    """
    ui = ctx.deps.ui
    steps = steps_for(ctx.features)
    ui.begin(heading(ctx.req), [TITLES[step.__name__] for step in steps])
    for number, step in enumerate(steps, 1):
        try:
            ui.stage(number)
            _run_step(ctx, number, step)
        except KeyboardInterrupt:
            ui.end("interrupted")
            _after_failure(ctx)
            raise
        except BaseException:
            ui.end("failed")
            _after_failure(ctx)
            raise
    ui.end("done")


def _run_step(ctx: DeployContext, number: int, step) -> None:
    """Run ``step`` and store the record; after a dropped connection, once more when the server answers."""
    try:
        step(ctx)
    except (SshError, CloudflareError) as err:
        if number < RERUNNABLE_FROM or not _dropped(err) or not _wait_for_network(ctx):
            raise
        ctx.deps.ui.info(f"与服务器的连接中断了，网络恢复后重跑这一步（{TITLES[step.__name__]}）。")
        step(ctx)
    _save(ctx)


def _save(ctx: DeployContext) -> None:
    """Store the record; a write that got no answer is tried once more, the step itself is done."""
    try:
        ctx.save()
    except CloudflareError as err:
        if not _dropped(err):
            raise
        ctx.deps.sleep(SAVE_RETRY_SECONDS)
        ctx.save()


def _dropped(err: Exception) -> bool:
    """A connection lost or never answered, as opposed to a refusal or a failed command."""
    if isinstance(err, SshError):
        return err.kind in ("port", "other")
    return err.status == 0


def _wait_for_network(ctx: DeployContext) -> bool:
    """Log in to the server until it works or ``NETWORK_WAIT_SECONDS`` have passed."""
    deps = ctx.deps
    deps.ui.detail(f"连接中断，等网络恢复（最多 {NETWORK_WAIT_SECONDS // 60} 分钟）。")
    deadline = Deadline(NETWORK_WAIT_SECONDS, deps.clock)
    while True:
        try:
            ctx.remote().run(["true"], timeout=LOGIN_TIMEOUT)
            return True
        except SshError:
            if deadline.passed():
                return False
            deps.sleep(deadline.step(NETWORK_RETRY_SECONDS))


def _after_failure(ctx: DeployContext) -> None:
    """Keep what the run did, and give the node back its SNI rotation if this run stopped it."""
    _save_after_failure(ctx)
    resume_rotation(ctx)


def _save_after_failure(ctx: DeployContext) -> None:
    try:
        ctx.save()
    except Exception as exc:  # the step's own error is the one to report
        ctx.deps.ui.warn(f"没能把节点的当前进度写入控制端：{exc}")
