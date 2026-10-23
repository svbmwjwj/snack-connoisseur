#!/usr/bin/env bash
# harden-cut.sh <new_port>
#
# Second half of cnsr's SSH hardening, run after a fresh login as admin on <new_port> worked:
# sshd keeps only <new_port>, root, password and keyboard-interactive logins are switched off
# and the firewall stops allowing 22.
#
# Before it changes anything it arms a timer: in 180 seconds systemd runs
# `cnsr-harden-revert stage`, which brings back the staged state (22 and <new_port> both
# open). The timer's accuracy is one second; systemd's default of one minute could let it
# fire after cnsr has finished waiting for it (200 seconds) and told you to rerun.
# cnsr stops the timer only after it has logged in again on <new_port>, so a cut that
# shuts cnsr out undoes itself. A timer or failed unit left from an earlier run is cleared
# first, because systemd-run refuses a unit name that is still loaded. Without the revert
# program the script stops before arming and changes nothing.
#
# After the timer: the final drop-in (the staged one comes back if sshd -t rejects it), then
# the firewall rule for 22 goes, then SSH restarts. main comes first in this file so that the
# order reads top to bottom; nothing above the timer may touch SSH or the firewall.
#
# Arrives on stdin of `bash -s`. All work happens in main, called on the last line with stdin
# on /dev/null: bash has read the whole script before the first command runs (a transfer
# cut short runs nothing), and no command reads from the SSH connection. Exits 0 or 1, never
# 255 (cnsr would take that for an SSH failure). Must parse and run under bash 3.2.

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

main() {
    local new_port=${1:-}
    check_port "$new_port"
    [ -x /usr/local/sbin/cnsr-harden-revert ] || unchanged "服务器上没有 /usr/local/sbin/cnsr-harden-revert，没有做任何改动。"

    systemctl stop cnsr-ssh-revert.timer cnsr-ssh-revert.service >/dev/null 2>&1 || true
    systemctl reset-failed cnsr-ssh-revert.timer cnsr-ssh-revert.service >/dev/null 2>&1 || true
    systemd-run --unit=cnsr-ssh-revert --on-active=180 --timer-property=AccuracySec=1s /usr/local/sbin/cnsr-harden-revert stage ||
        unchanged "没能启动自动恢复计时器，没有做任何改动。"

    DROP_IN=/etc/ssh/sshd_config.d/10-cnsr.conf
    set_drop_in "Port $new_port
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
" || exit 1
    ufw delete allow 22/tcp >/dev/null
    restart_ssh
}

fail() {
    echo "$*" >&2
    exit 1
}

# Exit 3 (cnsr.server.harden.UNCHANGED): stopped before the timer was armed, nothing changed.
unchanged() {
    echo "$*" >&2
    exit 3
}

check_port() {
    case "$1" in
        '' | *[!0-9]*) unchanged "新 SSH 端口不是数字：$1" ;;
    esac
    if [ "${#1}" -gt 5 ] || [ "$1" -lt 1024 ] || [ "$1" -gt 65535 ]; then
        unchanged "新 SSH 端口不在 1024-65535 之间：$1"
    fi
}

# put <path> <mode> <content>: replace <path> in one step (write a sibling, then rename it).
put() {
    local next="$1.cnsr-new"
    if ! { printf '%s' "$3" >"$next" && chmod "$2" "$next" && mv -f "$next" "$1"; }; then
        rm -f "$next"
        return 1
    fi
}

sshd_accepts() {
    # sshd -t refuses to run without its privilege separation directory.
    mkdir -p -m 0755 /run/sshd
    sshd -t
}

# set_drop_in <content>: replace cnsr's drop-in; when sshd -t rejects the result, put the
# previous one back (or remove it) and fail. SSH is not restarted here.
set_drop_in() {
    local saved="$DROP_IN.cnsr-old"
    rm -f "$saved"
    if [ -f "$DROP_IN" ]; then
        cp -p "$DROP_IN" "$saved" || return 1
    fi
    mkdir -p "${DROP_IN%/*}" && put "$DROP_IN" 644 "$1" || return 1
    if sshd_accepts; then
        rm -f "$saved"
        return 0
    fi
    if [ -f "$saved" ]; then
        mv -f "$saved" "$DROP_IN"
    else
        rm -f "$DROP_IN"
    fi
    echo "sshd 不接受新的 SSH 设置（sshd -t 未通过），已恢复原来的设置。" >&2
    return 1
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

main "$@" </dev/null
