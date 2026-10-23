#!/usr/bin/env bash
# verify-reality.sh <values_file>
#
# <values_file> holds the client's UUID, PUBLIC_KEY, SHORT_ID and SNI as KEY=value lines. cnsr
# writes it as a root-only file just before, so that the values never appear on a command line
# (sudo and the process list would show them); it is read as text, never sourced, and deleted
# on every way out.
#
# Proves from the node itself that the Xray container accepts a REALITY client: it starts a
# temporary client container (same image as the server, host network) that speaks VLESS +
# REALITY with flow xtls-rprx-vision to 127.0.0.1:443 and offers a socks5 proxy on
# 127.0.0.1:10808, then fetches https://www.gstatic.com/generate_204 through that proxy. It
# exits 0 only when the answer is HTTP 204, 1 when the handshake or the fetch failed, and 2 when
# the check itself could not run (no docker or curl, a client that never started or listened),
# with the reason (and the last lines of the client's log) on stderr. The client container and the temporary directory that
# holds its config are removed on every way out. The server container is not touched.
#
# The script arrives on stdin of `bash -s`, so no command may read stdin: main runs with
# stdin on /dev/null. The client config reaches the container as a file in a directory that
# only root can read, never on a command line. It never exits 255, which the caller would take
# for an SSH failure. It must also parse under bash 3.2 (macOS).

set -euo pipefail
export LC_ALL=C
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 077

# The image of the server (cnsr.server.xray.XRAY_IMAGE; a test keeps the two equal).
XRAY_IMAGE=ghcr.io/xtls/xray-core:26.3.27
SOCKS_PORT=10808
CONFIG_DIR=/usr/local/etc/xray-verify
PROBE_URL=https://www.gstatic.com/generate_204
WAIT_TRIES=30

UUID_PATTERN='^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$'
KEY_PATTERN='^[A-Za-z0-9_=-]+$'
SHORT_ID_PATTERN='^[0-9A-Fa-f]{1,16}$'
DOMAIN_PATTERN='^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$'

CONTAINER=""
WORKDIR=""
VALUES_FILE=""
UUID="" PUBLIC_KEY="" SHORT_ID="" SNI=""

# Runs on every way out: remove the client container, its config and the values file, and
# never leave with 255.
cleanup() {
    local status=$?
    trap - EXIT
    if [ -n "$CONTAINER" ]; then
        docker rm -f "$CONTAINER" </dev/null >/dev/null 2>&1 || true
    fi
    if [ -n "$WORKDIR" ]; then
        rm -rf "$WORKDIR"
    fi
    if [ -n "$VALUES_FILE" ]; then
        rm -f "$VALUES_FILE"
    fi
    if [ "$status" = 255 ]; then
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# fail <reason> [<status>]: exits 1, or <status> (2: the check itself could not run).
fail() {
    printf '%s\n' "$1" >&2
    exit "${2:-1}"
}

# read_values <file>: UUID, PUBLIC_KEY, SHORT_ID and SNI from its KEY=value lines; any other
# line is ignored.
read_values() {
    local line key
    [ -f "$1" ] || fail "找不到自检参数文件 $1。"
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        key=${line%%=*}
        case "$key" in
            UUID) UUID=${line#*=} ;;
            PUBLIC_KEY) PUBLIC_KEY=${line#*=} ;;
            SHORT_ID) SHORT_ID=${line#*=} ;;
            SNI) SNI=${line#*=} ;;
        esac
    done <"$1"
}

# write_config <uuid> <public_key> <short_id> <sni>: the client config in $WORKDIR. The
# arguments were checked against the patterns above, so nothing in them needs JSON escaping.
write_config() {
    cat >"$WORKDIR/config.json" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [
    {"tag": "socks", "listen": "127.0.0.1", "port": $SOCKS_PORT, "protocol": "socks", "settings": {"auth": "noauth", "udp": false}}
  ],
  "outbounds": [
    {
      "tag": "proxy",
      "protocol": "vless",
      "settings": {
        "vnext": [
          {"address": "127.0.0.1", "port": 443, "users": [{"id": "$1", "encryption": "none", "flow": "xtls-rprx-vision"}]}
        ]
      },
      "streamSettings": {
        "network": "raw",
        "security": "reality",
        "realitySettings": {"serverName": "$4", "fingerprint": "chrome", "publicKey": "$2", "shortId": "$3"}
      }
    }
  ]
}
EOF
}

# Waits up to WAIT_TRIES half-seconds for the socks proxy of the client to listen.
wait_for_socks() {
    local tries=0
    while [ "$tries" -lt "$WAIT_TRIES" ]; do
        if [ -n "$(ss -Hltn "sport = :$SOCKS_PORT" 2>/dev/null)" ]; then
            return 0
        fi
        sleep 0.5
        tries=$((tries + 1))
    done
    return 1
}

# Report a failure: the tail of the client's log first, the reason last (the caller shows the
# last lines of stderr).
refuse() {
    docker logs --tail 4 "$CONTAINER" </dev/null >&2 2>&1 || true
    fail "$@"
}

main() {
    local code
    if [ "$#" -ne 1 ]; then
        fail "用法：verify-reality.sh <参数文件>"
    fi
    VALUES_FILE=$1
    read_values "$VALUES_FILE"
    [[ $UUID =~ $UUID_PATTERN ]] || fail "UUID 的格式不对。"
    [[ $PUBLIC_KEY =~ $KEY_PATTERN ]] || fail "公钥的格式不对。"
    [[ $SHORT_ID =~ $SHORT_ID_PATTERN ]] || fail "shortId 的格式不对。"
    [[ $SNI =~ $DOMAIN_PATTERN ]] || fail "SNI 的格式不对：$SNI"
    command -v docker >/dev/null 2>&1 || fail "节点上没有 docker，无法做 REALITY 自检。" 2
    command -v curl >/dev/null 2>&1 || fail "节点上没有 curl，无法做 REALITY 自检。" 2

    WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/cnsr-verify.XXXXXX")
    write_config "$UUID" "$PUBLIC_KEY" "$SHORT_ID" "$SNI"
    CONTAINER="cnsr-verify-$$-$RANDOM"
    # The image runs Xray as a non-root user, which cannot read the root-only config: run the
    # short-lived client as root instead of loosening the config's permissions.
    docker run -d --name "$CONTAINER" --user 0:0 --network host -v "$WORKDIR:$CONFIG_DIR:ro" "$XRAY_IMAGE" \
        run -c "$CONFIG_DIR/config.json" >/dev/null || fail "无法启动自检用的客户端容器。" 2

    wait_for_socks || refuse "REALITY 自检失败：客户端在 $((WAIT_TRIES / 2)) 秒内没有开始监听 $SOCKS_PORT 端口。" 2
    code=$(curl --socks5-hostname "127.0.0.1:$SOCKS_PORT" --max-time 10 -s -o /dev/null -w '%{http_code}' "$PROBE_URL" 2>/dev/null || true)
    if [ "$code" != 204 ]; then
        refuse "REALITY 自检失败：经 443 端口访问 gstatic 得到 ${code:-空}（应为 204）。"
    fi
    printf '%s\n' "REALITY 自检通过。"
}

main "$@" </dev/null
exit 0
