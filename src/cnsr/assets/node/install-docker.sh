#!/usr/bin/env bash
# install-docker.sh
#
# Makes sure Docker Engine and the compose plugin are on the node, and jq, which the SNI
# rotation service needs. When `docker compose version` already works, Docker is not touched
# and a missing jq is installed from the system's own repositories. Otherwise the script adds
# Docker's official apt repository for the system (Debian or Ubuntu, by ID and
# VERSION_CODENAME in /etc/os-release, with the signing key under /etc/apt/keyrings), installs
# docker-ce, docker-ce-cli, containerd.io and docker-compose-plugin from it (with jq when it is
# missing), and enables and starts the docker service. Running it again changes nothing that
# is already in place.
#
# The script arrives on stdin of `bash -s`, so no command may read stdin: main runs with
# stdin on /dev/null. It exits 0 or 1 with a message on stderr, never 255, which the caller
# would take for an SSH failure. It must also parse under bash 3.2 (macOS).

set -euo pipefail
trap 'status=$?; if [ "$status" = 255 ]; then exit 1; fi' EXIT
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 022

OS_RELEASE=/etc/os-release
KEYRING_DIR=/etc/apt/keyrings
KEYRING=$KEYRING_DIR/docker.asc
SOURCE_LIST=/etc/apt/sources.list.d/docker.list
DOCKER_PACKAGES="docker-ce docker-ce-cli containerd.io docker-compose-plugin"
TOOL_PACKAGES="jq"
KEY_MARKER="BEGIN PGP PUBLIC KEY BLOCK"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

# put <path> <mode> <content>: replace <path> in one step (write a sibling, then rename it).
put() {
    local next="$1.cnsr-new"
    if ! { printf '%s' "$3" >"$next" && chmod "$2" "$next" && mv -f "$next" "$1"; }; then
        rm -f "$next"
        return 1
    fi
}

# A fresh server may still be running its first apt jobs, so apt waits for the lock a while.
apt_get() {
    apt-get -qq -o DPkg::Lock::Timeout=120 "$@" >/dev/null
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

# Sets REPO_URL (Docker's repository for this system) and SOURCE_LINE (its apt source line).
repository() {
    local id codename arch
    [ -r "$OS_RELEASE" ] || fail "找不到 ${OS_RELEASE}，无法判断系统。"
    # The file is a shell fragment written by the system; a subshell reads it.
    id=$(. "$OS_RELEASE" && printf '%s' "${ID:-}")
    codename=$(. "$OS_RELEASE" && printf '%s' "${VERSION_CODENAME:-}")
    case "$id" in
        debian | ubuntu) ;;
        *) fail "不支持的系统：${id:-未知}（只支持 Debian 和 Ubuntu）。" ;;
    esac
    [ -n "$codename" ] || fail "${OS_RELEASE} 里没有 VERSION_CODENAME，无法选择 Docker 软件源。"
    arch=$(dpkg --print-architecture)
    REPO_URL="https://download.docker.com/linux/$id"
    SOURCE_LINE="deb [arch=$arch signed-by=$KEYRING] $REPO_URL $codename stable"
}

install_repository() {
    local prerequisites key
    prerequisites=$(missing_packages ca-certificates curl)
    if [ -n "$prerequisites" ]; then
        apt_get update
        # $prerequisites is split on purpose: it only holds package names from this script.
        apt_get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y $prerequisites
    fi
    install -m 0755 -d "$KEYRING_DIR"
    mkdir -p "${SOURCE_LIST%/*}"
    key=$(curl -fsSL --retry 3 --max-time 60 "$REPO_URL/gpg") || fail "下载不到 Docker 的签名密钥：$REPO_URL/gpg"
    case "$key" in
        *"$KEY_MARKER"*) ;;
        *) fail "从 $REPO_URL/gpg 拿到的不是 PGP 公钥。" ;;
    esac
    put "$KEYRING" 644 "$key"$'\n'
    put "$SOURCE_LIST" 644 "$SOURCE_LINE"$'\n'
}

main() {
    local tools
    # $TOOL_PACKAGES is split on purpose: it only holds package names from this script.
    tools=$(missing_packages $TOOL_PACKAGES)
    if docker compose version >/dev/null 2>&1; then
        if [ -n "$tools" ]; then
            apt_get update
            apt_get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y $tools
        fi
        return 0
    fi
    repository
    install_repository
    apt_get update
    # $DOCKER_PACKAGES and $tools are split on purpose: they only hold package names from this script.
    apt_get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y $DOCKER_PACKAGES $tools
    systemctl enable --quiet --now docker
    docker compose version >/dev/null 2>&1 || fail "Docker 已安装，但 docker compose 仍然不能用。"
}

main "$@" </dev/null
exit 0
