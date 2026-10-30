"""Helpers for the ``cnsr rm`` tests: deploy a node through the fakes, then remove it."""

from __future__ import annotations

from deploy_fakes import VPS_IP, DeployFakes, run_up, run_rm

VPS_ANSWERS = ["", "", "已保存"]
CONFIRM_PROMPT = "确认删除服务器 jp-1？"


def deploy_aws(fakes: DeployFakes) -> None:
    """Deploy jp-1 on Lightsail through the fakes, then forget what that run did to them."""
    assert run_up(fakes, aws="jp-tokyo") == 0
    _forget_deploy(fakes)


def deploy_vps(fakes: DeployFakes) -> None:
    fakes.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fakes, ip=VPS_IP) == 0
    _forget_deploy(fakes)


def _forget_deploy(fakes: DeployFakes) -> None:
    fakes.lightsail.calls.clear()
    fakes.dns.requests.clear()
    fakes.server.log.clear()
    fakes.calls.clear()


def remove(fakes: DeployFakes, alias: str = "jp-1", answers: list[str] | None = None) -> tuple[int, str]:
    """Run ``rm`` with the confirmation picked; return the exit code and what it printed and logged."""
    terminal = fakes.terminal
    terminal.clear()
    terminal.inputs = ["1"] if answers is None else list(answers)
    code = run_rm(fakes, alias)
    return code, terminal.text


def entry_deleted(fakes: DeployFakes) -> bool:
    """Whether the control plane was asked to delete a node entry."""
    return any(method == "DELETE" and "/values/node:" in path for method, path in fakes.cloudflare.calls())
