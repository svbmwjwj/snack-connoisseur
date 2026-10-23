#!/bin/bash
# harden-revert.sh stage|all
#
# Undoes cnsr's SSH hardening. harden-stage.sh installs this file as
# /usr/local/sbin/cnsr-harden-revert, where the timer armed by harden-cut.sh runs it; cnsr
# also streams it to `bash -s` to roll back a staging that failed.
#
#   stage  back to the staged state: sshd listens on 22 and on the new port (read from cnsr's
#          drop-in), with the login settings it had before cnsr, and ufw allows 22 again
#   all    back to before cnsr: the ufw rules for the new ports are deleted (the drop-in's and
#          the ones listed in /opt/cnsr/.ufw-ports-by-cnsr), ufw is disabled when
#          cnsr enabled it (/opt/cnsr/.ufw-enabled-by-cnsr, written by harden-stage.sh), and
#          cnsr's drop-in and fail2ban jail are removed
#
# Every step is tried even when an earlier one failed, because getting SSH back matters more
# than stopping cleanly; the exit status is 1 if anything failed. SSH is restarted only after
# `sshd -t` accepts the configuration. If it rejects the one with cnsr's drop-in, the drop-in
# is removed and the server's own configuration is tried instead, so sshd is never restarted
# into a configuration it would not start with.
#
# Never reads stdin (main runs with stdin on /dev/null, also when streamed to `bash -s`) and
# never exits 255, which cnsr would take for an SSH failure. Must parse and run under bash 3.2.

set -euo pipefail
trap 'status=$?; if [ "$status" = 255 ]; then exit 1; fi' EXIT
export LC_ALL=C
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 022

# One hardening script at a time: a rerun after a dropped connection may find the first run
# still going on the server, and a rollback must not overlap the staging it undoes.
exec 9>/run/cnsr-harden.lock
if ! flock -w 60 9 </dev/null; then
    echo "另一个 cnsr 加固脚本仍在运行，等了 60 秒也没有结束。" >&2
    exit 1
fi

DROP_IN=/etc/ssh/sshd_config.d/10-cnsr.conf
JAIL=/etc/fail2ban/jail.d/cnsr.local
UFW_MARKER=/opt/cnsr/.ufw-enabled-by-cnsr
UFW_PORTS=/opt/cnsr/.ufw-ports-by-cnsr  # the new ports harden-stage.sh opened
failed=0

warn() {
    echo "cnsr-harden-revert：$*" >&2
}

# put <path> <mode> <content>: replace <path> in one step (write a sibling, then rename it).
put() {
    local next="$1.cnsr-new"
    if ! { printf '%s' "$3" >"$next" && chmod "$2" "$next" && mv -f "$next" "$1"; }; then
        rm -f "$next"
        return 1
    fi
}

# The ports in cnsr's drop-in other than 22, one per line: the new SSH port.
new_ports() {
    local key value rest
    [ -f "$DROP_IN" ] || return 0
    while read -r key value rest || [ -n "$key" ]; do
        if [ "$key" = Port ] && [ "$value" != 22 ]; then
            case "$value" in
                '' | *[!0-9]*) ;;
                *) printf '%s\n' "$value" ;;
            esac
        fi
    done <"$DROP_IN"
}

sshd_accepts() {
    # sshd -t refuses to run without its privilege separation directory.
    mkdir -p -m 0755 /run/sshd
    sshd -t
}

# On Ubuntu 22.10 and later ssh.socket owns the listening ports and is regenerated from the
# sshd configuration on daemon-reload.
restart_ssh() {
    if systemctl is-active --quiet ssh.socket; then
        systemctl daemon-reload && systemctl restart ssh.socket
    else
        systemctl restart ssh
    fi
}

# apply_sshd <content>: make cnsr's drop-in hold <content> (remove it when empty), then
# restart SSH. Fails when sshd rejected the drop-in, even if SSH came back without it.
apply_sshd() {
    if [ -n "$1" ]; then
        mkdir -p "${DROP_IN%/*}" && put "$DROP_IN" 644 "$1" || return 1
    else
        rm -f "$DROP_IN" || return 1
    fi
    if sshd_accepts; then
        restart_ssh
        return
    fi
    rm -f "$DROP_IN"
    if ! sshd_accepts; then
        warn "sshd 不接受系统原有的 SSH 配置，没有重启 SSH。"
        return 1
    fi
    warn "sshd 不接受 cnsr 的 SSH 设置，已删除它，按系统原有的 SSH 配置重启。"
    restart_ssh
    return 1
}

revert_stage() {
    local ports port content
    ports=$(new_ports)
    if command -v ufw >/dev/null 2>&1; then
        ufw allow 22/tcp >/dev/null || failed=1
        for port in $ports; do
            ufw allow "$port/tcp" >/dev/null || failed=1
        done
    fi
    content="Port 22
"
    for port in $ports; do
        content="${content}Port $port
"
    done
    apply_sshd "$content" || failed=1
}

revert_all() {
    local port seen=" "
    if command -v ufw >/dev/null 2>&1; then
        for port in $(new_ports) $(grep -x '[0-9][0-9]*' "$UFW_PORTS" 2>/dev/null || true); do
            case "$seen" in *" $port "*) continue ;; esac
            seen="$seen$port "
            ufw delete allow "$port/tcp" >/dev/null || failed=1
        done
        [ "$failed" = 1 ] || rm -f "$UFW_PORTS"
        # A ufw that was on before cnsr stays on; the marker goes only once ufw is off.
        if [ -f "$UFW_MARKER" ]; then
            if ufw disable >/dev/null; then
                rm -f "$UFW_MARKER" || failed=1
            else
                failed=1
            fi
        fi
    fi
    apply_sshd "" || failed=1
    if [ -f "$JAIL" ]; then
        rm -f "$JAIL" || failed=1
        # Without cnsr's jail fail2ban may not start again (Debian 12 has no auth.log for its
        # default sshd jail). That does not touch SSH access, so it is only a warning.
        systemctl try-restart fail2ban || warn "fail2ban 没能重新启动。"
    fi
}

main() {
    case "${1:-}" in
        stage) revert_stage ;;
        all) revert_all ;;
        *)
            echo "用法：cnsr-harden-revert stage|all" >&2
            return 2
            ;;
    esac
    return "$failed"
}

main "$@" </dev/null
