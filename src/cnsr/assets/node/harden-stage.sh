#!/usr/bin/env bash
# harden-stage.sh <new_port> <pubkey_line> <revert_program_base64>
#
# First half of cnsr's SSH hardening, for a server cnsr reached as root. Afterwards sshd
# listens on 22 and on <new_port>, and the new user admin can log in with <pubkey_line> and
# run sudo without a password. Nothing that lets anyone log in today is taken away yet:
# harden-cut.sh closes 22 later, after cnsr has logged in as admin on <new_port>.
#
# Steps, in order; every one is safe to repeat, and so is the whole script:
#   1. install the revert program /usr/local/sbin/cnsr-harden-revert from the third argument
#      (harden-revert.sh in base64, filled in by cnsr) once it parses; first, so that it is
#      there for any rollback
#   2. install ufw, fail2ban, python3-systemd, unattended-upgrades and sudo when missing
#   3. user admin with the key; passwordless sudo, checked by visudo before it is installed
#   4. automatic security updates (/etc/apt/apt.conf.d/20auto-upgrades)
#   5. cnsr's sshd drop-in with Port 22 and Port <new_port>; SSH restarts only after sshd -t
#      accepts it (otherwise the previous drop-in comes back) and sshd -T shows the new port
#   6. ufw allows 22/tcp, <new_port>/tcp, 443/tcp and 443/udp, then denies other incoming
#      traffic and is enabled; the allow rules come first so that 22 is never shut. When ufw
#      was off, /opt/cnsr/.ufw-enabled-by-cnsr records that cnsr turned it on, so that
#      `harden-revert.sh all` turns off only a ufw cnsr enabled. /opt/cnsr/.ufw-ports-by-cnsr
#      lists the new ports cnsr opened: a rule an earlier, interrupted stage opened for
#      another port is deleted again
#   7. a fail2ban jail for sshd on <new_port>, reading the systemd journal
#
# Arrives on stdin of `bash -s`. All work happens in main, called on the last line with stdin
# on /dev/null: bash has read the whole script before the first command runs (a transfer
# cut short runs nothing), and no command reads from the SSH connection. It stops at the first
# failure with a non-zero status, never 255 (cnsr would take that for an SSH failure); cnsr
# then runs `harden-revert.sh all`. Must parse and run under bash 3.2.

set -euo pipefail
trap 'status=$?; if [ "$status" = 255 ]; then exit 1; fi' EXIT
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 022

# One hardening script at a time: a rerun after a dropped connection may find the first run
# still going on the server, and a rollback must not overlap the staging it undoes.
exec 9>/run/cnsr-harden.lock
if ! flock -w 60 9 </dev/null; then
    echo "另一个 cnsr 加固脚本仍在运行，等了 60 秒也没有结束。" >&2
    exit 1
fi

ADMIN=admin
ADMIN_HOME=/home/admin
REVERT_PROGRAM=/usr/local/sbin/cnsr-harden-revert
SUDOERS=/etc/sudoers.d/90-cnsr-admin
AUTO_UPGRADES=/etc/apt/apt.conf.d/20auto-upgrades
DROP_IN=/etc/ssh/sshd_config.d/10-cnsr.conf
JAIL=/etc/fail2ban/jail.d/cnsr.local
UFW_MARKER=/opt/cnsr/.ufw-enabled-by-cnsr
UFW_PORTS=/opt/cnsr/.ufw-ports-by-cnsr
AUTO_UPGRADES_TEXT='APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
'

fail() {
    echo "$*" >&2
    exit 1
}

check_args() {
    case "$1" in
        '' | *[!0-9]*) fail "新 SSH 端口不是数字：$1" ;;
    esac
    if [ "${#1}" -gt 5 ] || [ "$1" -lt 1024 ] || [ "$1" -gt 65535 ]; then
        fail "新 SSH 端口不在 1024-65535 之间：$1"
    fi
    case "$2" in
        '' | *$'\n'* | *$'\r'*) fail "公钥必须是单独的一行。" ;;
    esac
    [ -n "$3" ] || fail "缺少回滚程序，没有做任何改动。"
}

# put <path> <mode> <content>: replace <path> in one step (write a sibling, then rename it).
put() {
    local next="$1.cnsr-new"
    if ! { printf '%s' "$3" >"$next" && chmod "$2" "$next" && mv -f "$next" "$1"; }; then
        rm -f "$next"
        return 1
    fi
}

install_revert_program() {
    local next="$REVERT_PROGRAM.cnsr-new"
    mkdir -p "${REVERT_PROGRAM%/*}"
    if ! printf '%s' "$1" | base64 -d >"$next" 2>/dev/null || ! bash -n "$next" 2>/dev/null; then
        rm -f "$next"
        fail "回滚程序无效（不是 base64 或无法解析），没有做任何改动。"
    fi
    chmod 755 "$next"
    mv -f "$next" "$REVERT_PROGRAM"
}

# The packages among the arguments that are not installed yet, separated by spaces.
missing_packages() {
    local package missing=""
    for package in "$@"; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -x 'install ok installed' >/dev/null; then
            missing="$missing $package"
        fi
    done
    printf '%s' "${missing# }"
}

# A fresh server may still be running its first apt jobs, so apt waits for the lock a while.
install_packages() {
    local missing
    missing=$(missing_packages "$@")
    [ -n "$missing" ] || return 0
    apt-get -qq -o DPkg::Lock::Timeout=120 update >/dev/null
    # $missing is split on purpose: it only holds package names from this script.
    apt-get -qq -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        install -y $missing >/dev/null
}

add_admin() {
    local keys="$ADMIN_HOME/.ssh/authorized_keys" rule="$SUDOERS.cnsr-new"
    if ! id "$ADMIN" >/dev/null 2>&1; then
        # useradd refuses to create the user's own group when one of that name exists.
        if getent group "$ADMIN" >/dev/null 2>&1; then
            useradd -m -d "$ADMIN_HOME" -s /bin/bash -g "$ADMIN" "$ADMIN"
        else
            useradd -m -d "$ADMIN_HOME" -s /bin/bash "$ADMIN"
        fi
    fi
    mkdir -p "$ADMIN_HOME/.ssh"
    touch "$keys"
    if ! grep -qxF -- "$1" "$keys"; then
        # Without a final newline the key would be glued onto the last one.
        if [ -s "$keys" ] && [ -n "$(tail -c 1 "$keys")" ]; then
            printf '\n' >>"$keys"
        fi
        printf '%s\n' "$1" >>"$keys"
    fi
    chmod 700 "$ADMIN_HOME/.ssh"
    chmod 600 "$keys"
    chown -R "$ADMIN:" "$ADMIN_HOME/.ssh"
    # sudo ignores files with a dot in their name, so the rule is inert until it is renamed.
    mkdir -p "${SUDOERS%/*}"
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$ADMIN" >"$rule"
    chmod 440 "$rule"
    if ! visudo -cf "$rule" >/dev/null; then
        rm -f "$rule"
        fail "sudo 规则没有通过 visudo 检查，没有安装。"
    fi
    mv -f "$rule" "$SUDOERS"
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

# A sshd_config without the Include of sshd_config.d would accept the drop-in and ignore it.
listen_on_both() {
    set_drop_in "Port 22
Port $1
" || exit 1
    if ! sshd -T | grep -x "port $1" >/dev/null; then
        fail "sshd 没有读取 /etc/ssh/sshd_config.d 里的设置，无法安全地改 SSH 端口。"
    fi
    restart_ssh
}

firewall() {
    local status port
    status=$(ufw status 2>/dev/null || true)
    case "$status" in
        "Status: active"*) ;;
        *)
            mkdir -p "${UFW_MARKER%/*}"
            : >"$UFW_MARKER"
            ;;
    esac
    # Listed before it is opened, so a rule cnsr added is never forgotten.
    mkdir -p "${UFW_PORTS%/*}"
    grep -qx "$1" "$UFW_PORTS" 2>/dev/null || printf '%s\n' "$1" >>"$UFW_PORTS"
    ufw allow 22/tcp >/dev/null
    ufw allow "$1/tcp" >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw allow 443/udp >/dev/null
    ufw default deny incoming >/dev/null
    ufw --force enable >/dev/null
    for port in $(grep -x '[0-9][0-9]*' "$UFW_PORTS" || true); do
        [ "$port" = "$1" ] || ufw delete allow "$port/tcp" >/dev/null
    done
    put "$UFW_PORTS" 600 "$1
"
}

fail2ban_jail() {
    mkdir -p "${JAIL%/*}"
    put "$JAIL" 644 "[sshd]
enabled = true
port = $1
backend = systemd
"
    systemctl enable --quiet fail2ban
    systemctl restart fail2ban
}

main() {
    local new_port=${1:-} pubkey=${2:-} program=${3:-}
    check_args "$new_port" "$pubkey" "$program"
    install_revert_program "$program"
    install_packages ufw fail2ban python3-systemd unattended-upgrades sudo
    add_admin "$pubkey"
    mkdir -p "${AUTO_UPGRADES%/*}"
    put "$AUTO_UPGRADES" 644 "$AUTO_UPGRADES_TEXT"
    listen_on_both "$new_port"
    firewall "$new_port"
    fail2ban_jail "$new_port"
}

main "$@" </dev/null
