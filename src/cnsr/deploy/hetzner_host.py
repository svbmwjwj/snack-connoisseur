"""A Hetzner node's server and cloud firewall: the parts of steps 1 and 3 that differ from Lightsail.

The Primary IP ``<alias>-ip`` is made and recorded before the server, so that a run killed
during the create leaves ``rm`` something to find. The firewall ``<alias>-fw`` is applied
before hardening starts; while SSH moves to its new port it lets both ports in, and 22 is
dropped only once hardening has succeeded (see ``Hardener.run``). A blocked Primary IP is
swapped for a new one while the server is off (see ``reach.swap_ip``).
"""

from __future__ import annotations

from cnsr.deploy.context import DeployContext, hetzner_elsewhere
from cnsr.errors import CnsrError
from cnsr import sizes
from cnsr.hetzner.cloud import stranger_server
from cnsr.hetzner.labels import server_name

STATIC_IP_SUFFIX = "-ip"
FIREWALL_SUFFIX = "-fw"
SWAP_SUFFIX = "-new"  # the new Primary IP's name while the blocked one still holds <alias>-ip


def ensure_server(ctx: DeployContext) -> None:
    """Create the node's Primary IP and server, or accept the ones cnsr created earlier; record them.

    A server of the name without cnsr's label stops the run before anything is created or
    recorded. A rerun whose server is gone creates it again as the recorded tier's cheapest
    type, shown on screen without asking, and deploys it as a first deployment: the new server
    knows only root on 22 and has nothing on it.
    """
    node, hetzner, ui = ctx.node, ctx.hetzner, ctx.deps.ui
    name = node.instance_name or server_name(node.alias)
    ip_name = node.static_ip_name or f"{node.alias}{STATIC_IP_SUFFIX}"
    ui.detail(f"准备 Hetzner 服务器 {name}（区域 {ctx.req.region}，已有就沿用）。")
    tagged = hetzner.instance_tagged(name, node.alias)
    if tagged is False:
        raise CnsrError(stranger_server(name, node.alias))
    offer = ctx.offer
    if offer is None and tagged is None:
        elsewhere = hetzner_elsewhere(ctx.deps, ctx.req.region, name, node.alias)
        if elsewhere:
            raise CnsrError(elsewhere)
        offer = hetzner.pick(node.size)
        ui.info(f"服务器 {name} 不在了，按记录的规格重新开一台：{sizes.describe(offer)}")
        _start_over(ctx)
    key = hetzner.ensure_key_pair(node.ssh.key_name, ctx.key.line)
    ipv4 = hetzner.ensure_primary_ip(ip_name, node.alias)
    # On record before the server exists: a run killed outright right after the create leaves rm a name.
    node.static_ip_name, node.instance_name = ip_name, name
    ctx.save()
    ctx.new_instance = hetzner.ensure_instance(name, key, offer, ip_name, node.alias)
    hetzner.wait_running(name)
    node.ipv6 = hetzner.instance_ipv6(name)
    if ctx.new_instance:
        # A new server has a new host key: forget what this machine remembers for its names.
        for host in (ipv4, node.domain):
            if host:
                ctx.deps.forget_host_key(host, node.ssh.port)
    node.ipv4 = ipv4


def _start_over(ctx: DeployContext) -> None:
    """Treat the node as never deployed: log in as root on 22, and clean up should the run fail."""
    node = ctx.node
    node.ssh.user, node.ssh.port = "root", 22
    node.features = []
    ctx.in_use = ctx.deployed = False


def swap_primary_ip(ctx: DeployContext) -> None:
    """Give the node's server, powered off meanwhile, a new Primary IP in place of its blocked one.

    The new IP is created before the blocked one is deleted, so its address always differs.
    The record names the IP that would be left behind on its own: the blocked one until it is
    deleted (the new one is assigned to the server and goes with it), then the new one.
    """
    node, hetzner = ctx.node, ctx.hetzner
    server, blocked = node.instance_name, node.static_ip_name
    final = f"{node.alias}{STATIC_IP_SUFFIX}"
    temporary = f"{final}{SWAP_SUFFIX}"
    hetzner.power_off(server)  # a hard power-off: nothing runs on the server yet
    hetzner.unassign_primary_ip(blocked)
    ipv4 = hetzner.create_assigned_primary_ip(temporary, node.alias, server)
    errors = hetzner.delete_primary_ip(blocked, node.alias)
    if errors:
        raise CnsrError(f"没能删除被墙的 Primary IP {node.ipv4}：{'；'.join(errors)}")
    node.static_ip_name, node.ipv4 = temporary, ipv4
    ctx.save()
    hetzner.rename_primary_ip(temporary, final)
    node.static_ip_name = final
    ctx.save()
    hetzner.power_on(server)


def apply_firewall(ctx: DeployContext) -> None:
    """Make the node's firewall exist with rules for the current login and the chosen features,
    then apply it to the server.

    The rules come first, so the server is never behind a firewall that drops SSH. While the
    login is still root, 22 stays open next to the recorded port.
    """
    node = ctx.node
    ctx.hetzner.ensure_firewall(firewall_name(ctx), node.alias)
    allow_ssh(ctx, *([22] if node.ssh.user == "root" else []), node.ssh.port)
    ctx.hetzner.attach_firewall(firewall_name(ctx), node.instance_name or server_name(node.alias))


def allow_ssh(ctx: DeployContext, *ssh_ports: int) -> None:
    """Let exactly ``ssh_ports`` and the chosen features' ports in, once Hetzner has applied it."""
    name = firewall_name(ctx)
    ssh_ports = tuple(dict.fromkeys(ssh_ports))
    ctx.hetzner.set_rules(name, ssh_ports, ctx.app_ports)
    numbers = [str(port) for port in dict.fromkeys([*ssh_ports, *(port for port, _ in ctx.app_ports)])]
    *first, last = numbers
    opened = f"{'、'.join(first)} 和 {last}" if first else last
    ctx.deps.ui.detail(f"Hetzner 防火墙 {name} 只放行 {opened}。")


def firewall_name(ctx: DeployContext) -> str:
    return f"{ctx.node.alias}{FIREWALL_SUFFIX}"
