#!/usr/bin/env bash
# validate-candidates.sh <domain> <ip> [<domain> <ip> ...]
#
# Checks from the node itself which SNI candidates a REALITY server could borrow: for every
# pair it fetches https://<domain>/ from <ip> with TLS 1.3 only, X25519 and HTTP/2. A pair
# that completes the request with HTTP/2 is printed as `domain ip ms` on stdout, ms being the
# TLS connect time in whole milliseconds; every other pair is left out. src/cnsr/scan/select.py
# reads the output. The one change on the node: curl and ca-certificates are installed first
# when they are missing (minimal images may lack them).
#
# The script arrives on stdin of `bash -s`, so no command may read stdin: main runs with
# stdin on /dev/null, and the candidates come in as arguments. It exits 0 whether or not any
# pair passed, or 1 with a message on stderr when the arguments are not pairs, curl cannot be
# installed, or curl itself cannot work (it failed for a reason that has nothing to do with
# the candidate, so "no candidate passed" would be a lie). It never exits 255, which the
# caller would take for an SSH failure. It must also parse under bash 3.2 (macOS).

set -euo pipefail
trap 'status=$?; if [ "$status" = 255 ]; then exit 1; fi' EXIT
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

DOMAIN_PATTERN='^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$'
IP_PATTERN='^[0-9A-Fa-f:.]+$'
# curl's `%{http_version} %{time_appconnect}` for a request that used HTTP/2: "2 0.123456".
HTTP2_PATTERN='^2 ([0-9]+(\.[0-9]+)?)$'

fail() {
    printf '%s\n' "$1" >&2
    exit 1
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

# curl and the CA certificates it checks the candidates' certificates with. A fresh server may
# still be running its first apt jobs, so apt waits for the lock a while.
ensure_curl() {
    local missing
    missing=$(missing_packages curl ca-certificates)
    [ -n "$missing" ] || return 0
    # $missing is split on purpose: it only holds package names from this script.
    if ! { apt-get -qq -o DPkg::Lock::Timeout=120 update >/dev/null &&
        apt-get -qq -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
            install -y $missing >/dev/null; }; then
        fail "节点上缺少 $missing，自动安装失败，无法验证候选。"
    fi
}

# curl_broken <status>: curl failed before it ever talked to the candidate.
curl_broken() {
    local reason
    case "$1" in
        2) reason="初始化失败，可能不支持 --curves 等选项" ;;
        4) reason="缺少所需的功能（TLS 1.3、X25519 或 HTTP/2）" ;;
        *) reason="找不到 curl 命令" ;;
    esac
    fail "节点上的 curl 无法完成验证（退出码 $1：$reason），这与候选无关，请检查节点上的 curl。"
}

# milliseconds <seconds>: curl's "0.123456" as whole milliseconds, "123".
milliseconds() {
    local whole=${1%%.*} fraction=""
    case "$1" in
        *.*) fraction=${1#*.} ;;
    esac
    fraction="${fraction}000"
    fraction=${fraction:0:3}
    printf '%s' "$((10#$whole * 1000 + 10#$fraction))"
}

# check <domain> <ip>: print `domain ip ms` when the pair passes. A pair that does not pass
# is no error; it simply prints nothing. A curl that cannot work at all stops the script.
check() {
    local domain=$1 ip=$2 output status=0
    if ! [[ $domain =~ $DOMAIN_PATTERN && $ip =~ $IP_PATTERN ]]; then
        printf '跳过无法识别的候选：%s %s\n' "$domain" "$ip" >&2
        return 0
    fi
    output=$(curl -sS -o /dev/null --max-time 8 --resolve "$domain:443:$ip" --tlsv1.3 --tls-max 1.3 \
        --curves X25519 --http2 -w '%{http_version} %{time_appconnect}' "https://$domain/" 2>/dev/null) || status=$?
    case "$status" in
        0) ;;
        2 | 4 | 127) curl_broken "$status" ;;
        *) return 0 ;;
    esac
    if [[ $output =~ $HTTP2_PATTERN ]]; then
        printf '%s %s %s\n' "$domain" "$ip" "$(milliseconds "${BASH_REMATCH[1]}")"
    fi
}

main() {
    if [ "$#" -eq 0 ] || [ $(($# % 2)) -ne 0 ]; then
        fail "用法：validate-candidates.sh <域名> <IP> [<域名> <IP> ...]，参数必须成对出现。"
    fi
    ensure_curl
    command -v curl >/dev/null 2>&1 || fail "节点上没有 curl，无法验证候选。"
    while [ "$#" -gt 0 ]; do
        check "$1" "$2"
        shift 2
    done
}

main "$@" </dev/null
exit 0
