#!/usr/bin/env bash
# baseline-check.sh
#
# The fixed baseline for a Lightsail server: turn on automatic security updates (install
# unattended-upgrades when missing, write /etc/apt/apt.conf.d/20auto-upgrades, enable the
# service), then print on stdout, for src/cnsr/server/baseline.py to check, the settings sshd
# uses for a login as admin and as root: a line `cnsr-context <user>` followed by
# `sshd -T -C user=<user>,host=cnsr,addr=192.0.2.1`, so that Match blocks are applied. SSH is
# only read, never changed; everything else the script runs prints to stderr or nowhere.
#
# Arrives on stdin of `bash -s`. All work happens in main, called on the last line with stdin
# on /dev/null: bash has read the whole script before the first command runs (a transfer
# cut short runs nothing), and no command reads from the SSH connection. Exits 0 or 1, never
# 255 (cnsr would take that for an SSH failure, and sshd -T itself exits 255 on a
# configuration error). Must parse and run under bash 3.2.

set -euo pipefail
trap 'status=$?; if [ "$status" = 255 ]; then exit 1; fi' EXIT
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 022

AUTO_UPGRADES=/etc/apt/apt.conf.d/20auto-upgrades
AUTO_UPGRADES_TEXT='APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
'

# put <path> <mode> <content>: replace <path> in one step (write a sibling, then rename it).
put() {
    local next="$1.cnsr-new"
    if ! { printf '%s' "$3" >"$next" && chmod "$2" "$next" && mv -f "$next" "$1"; }; then
        rm -f "$next"
        return 1
    fi
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

main() {
    install_packages unattended-upgrades
    mkdir -p "${AUTO_UPGRADES%/*}"
    put "$AUTO_UPGRADES" 644 "$AUTO_UPGRADES_TEXT"
    systemctl enable --quiet --now unattended-upgrades
    # sshd -T refuses to run without its privilege separation directory.
    mkdir -p -m 0755 /run/sshd
    local user
    for user in admin root; do
        printf 'cnsr-context %s\n' "$user"
        sshd -T -C "user=$user,host=cnsr,addr=192.0.2.1"
    done
}

main "$@" </dev/null
