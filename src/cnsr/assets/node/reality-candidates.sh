#!/usr/bin/env bash
# reality-candidates.sh <domain> <ip> [<domain> <ip> ...]
#
# Checks from the node, with the Xray image the node runs, which SNI candidates a REALITY server
# can really borrow. validate-candidates.sh only proves a TLS 1.3 + X25519 + h2 request works;
# REALITY asks more of the target's handshake (the REALITY in Xray 26.3.27, for one, gives up on
# a target whose certificate record is over 8192 bytes), so each pair is tried for real: one
# temporary server container holds a REALITY inbound per pair on 127.0.0.1:<SERVER_BASE + n>
# that borrows <domain> from <ip>:443, and one temporary client container offers a socks5 proxy
# per pair on 127.0.0.1:<SOCKS_BASE + n> that speaks VLESS + REALITY with flow
# xtls-rprx-vision to the matching inbound. https://www.gstatic.com/generate_204 is fetched
# through each proxy; a pair that gets HTTP 204 is printed as `domain ip` on stdout, in the
# order given, and every other pair is left out. The key pair, UUID and shortId are made up for
# this check and thrown away.
#
# It exits 0 whether or not any pair passed, 1 when the arguments are not pairs, and 2 when the
# check itself could not run (no docker or curl, a container that never started or listened),
# with the reason on stderr. Both containers and their configs are removed on every way out.
#
# The script arrives on stdin of `bash -s`, so no command may read stdin: main runs with stdin
# on /dev/null. It never exits 255, which the caller would take for an SSH failure. It must
# also parse under bash 3.2 (macOS).

set -euo pipefail
export LC_ALL=C
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 077

# The image of the server (cnsr.server.xray.XRAY_IMAGE; a test keeps the two equal).
XRAY_IMAGE=ghcr.io/xtls/xray-core:26.3.27
SERVER_BASE=11000
SOCKS_BASE=12000
MAX_PAIRS=200
CONFIG_DIR=/usr/local/etc/xray-candidates
PROBE_URL=https://www.gstatic.com/generate_204
WAIT_TRIES=30

DOMAIN_PATTERN='^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$'
IP_PATTERN='^[0-9A-Fa-f:.]+$'
KEY_PATTERN='^[A-Za-z0-9_-]{43}$'

SERVER="" CLIENT="" WORKDIR=""
DOMAINS=() IPS=()

# Runs on every way out: remove both containers and the configs, and never leave with 255.
cleanup() {
    local status=$?
    trap - EXIT
    for container in "$SERVER" "$CLIENT"; do
        if [ -n "$container" ]; then
            docker rm -f "$container" </dev/null >/dev/null 2>&1 || true
        fi
    done
    if [ -n "$WORKDIR" ]; then
        rm -rf "$WORKDIR"
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

# hex <count>: <count> random hex digits; good enough for a throwaway identity.
hex() {
    local out=""
    while [ "${#out}" -lt "$1" ]; do
        out="$out$(printf '%04x' "$RANDOM")"
    done
    printf '%s' "${out:0:$1}"
}

# target <ip>: the REALITY target for <ip>, an IPv6 address in brackets.
target() {
    case "$1" in
        *:*) printf '[%s]:443' "$1" ;;
        *) printf '%s:443' "$1" ;;
    esac
}

# write_configs <private_key> <public_key> <uuid> <short_id>: the server and client configs in
# $WORKDIR/server and $WORKDIR/client. Every value was checked against a pattern above, so
# nothing in them needs JSON escaping.
write_configs() {
    local n inbounds="" socks="" outbounds="" rules="" sep=""
    for n in "${!DOMAINS[@]}"; do
        inbounds="$inbounds$sep{\"listen\": \"127.0.0.1\", \"port\": $((SERVER_BASE + n)), \"protocol\": \"vless\",
      \"settings\": {\"clients\": [{\"id\": \"$3\", \"flow\": \"xtls-rprx-vision\"}], \"decryption\": \"none\"},
      \"streamSettings\": {\"network\": \"raw\", \"security\": \"reality\", \"realitySettings\": {
        \"target\": \"$(target "${IPS[$n]}")\", \"serverNames\": [\"${DOMAINS[$n]}\"], \"privateKey\": \"$1\", \"shortIds\": [\"$4\"]}}}"
        socks="$socks$sep{\"tag\": \"socks$n\", \"listen\": \"127.0.0.1\", \"port\": $((SOCKS_BASE + n)), \"protocol\": \"socks\",
      \"settings\": {\"auth\": \"noauth\", \"udp\": false}}"
        outbounds="$outbounds$sep{\"tag\": \"proxy$n\", \"protocol\": \"vless\",
      \"settings\": {\"vnext\": [{\"address\": \"127.0.0.1\", \"port\": $((SERVER_BASE + n)),
        \"users\": [{\"id\": \"$3\", \"encryption\": \"none\", \"flow\": \"xtls-rprx-vision\"}]}]},
      \"streamSettings\": {\"network\": \"raw\", \"security\": \"reality\", \"realitySettings\": {
        \"serverName\": \"${DOMAINS[$n]}\", \"fingerprint\": \"chrome\", \"publicKey\": \"$2\", \"shortId\": \"$4\"}}}"
        rules="$rules$sep{\"type\": \"field\", \"inboundTag\": [\"socks$n\"], \"outboundTag\": \"proxy$n\"}"
        sep=",
    "
    done
    mkdir -p "$WORKDIR/server" "$WORKDIR/client"
    cat >"$WORKDIR/server/config.json" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [
    $inbounds
  ],
  "outbounds": [{"protocol": "freedom"}]
}
EOF
    cat >"$WORKDIR/client/config.json" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [
    $socks
  ],
  "outbounds": [
    $outbounds
  ],
  "routing": {"rules": [
    $rules
  ]}
}
EOF
}

# start <name> <dir>: a container running Xray with <dir>/config.json. Root inside: the image
# runs Xray as a non-root user, which cannot read the root-only config.
start() {
    docker run -d --name "$1" --user 0:0 --network host -v "$2:$CONFIG_DIR:ro" "$XRAY_IMAGE" \
        run -c "$CONFIG_DIR/config.json" >/dev/null
}

# listening <port>: waits up to WAIT_TRIES half-seconds for something to listen on <port>.
listening() {
    local tries=0
    while [ "$tries" -lt "$WAIT_TRIES" ]; do
        if [ -n "$(ss -Hltn "sport = :$1" 2>/dev/null)" ]; then
            return 0
        fi
        sleep 0.5
        tries=$((tries + 1))
    done
    return 1
}

main() {
    local keys line private_key="" public_key="" uuid short_id last n code
    if [ "$#" -eq 0 ] || [ $(($# % 2)) -ne 0 ]; then
        fail "用法：reality-candidates.sh <域名> <IP> [<域名> <IP> ...]，参数必须成对出现。"
    fi
    while [ "$#" -gt 0 ]; do
        if [[ $1 =~ $DOMAIN_PATTERN && $2 =~ $IP_PATTERN ]]; then
            DOMAINS+=("$1")
            IPS+=("$2")
        else
            printf '跳过无法识别的候选：%s %s\n' "$1" "$2" >&2
        fi
        shift 2
    done
    [ "${#DOMAINS[@]}" -gt 0 ] || return 0
    [ "${#DOMAINS[@]}" -le "$MAX_PAIRS" ] || fail "候选太多：最多 $MAX_PAIRS 个。"
    command -v docker >/dev/null 2>&1 || fail "节点上没有 docker，无法做 REALITY 候选检查。" 2
    command -v curl >/dev/null 2>&1 || fail "节点上没有 curl，无法做 REALITY 候选检查。" 2

    # Both layouts of `xray x25519`: PrivateKey/Password, and Private key/Public key.
    keys=$(docker run --rm "$XRAY_IMAGE" x25519 </dev/null) || fail "无法用 Xray 生成检查用的密钥。" 2
    while IFS= read -r line; do
        case "$line" in
            PrivateKey:* | "Private key:"*) private_key=${line#*: } ;;
            Password*:* | "Public key:"*) public_key=${line#*: } ;;
        esac
    done <<<"$keys"
    [[ $private_key =~ $KEY_PATTERN && $public_key =~ $KEY_PATTERN ]] || fail "无法读出 Xray 生成的密钥。" 2
    uuid="$(hex 8)-$(hex 4)-4$(hex 3)-8$(hex 3)-$(hex 12)"
    short_id=$(hex 16)

    WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/cnsr-candidates.XXXXXX")
    write_configs "$private_key" "$public_key" "$uuid" "$short_id"
    SERVER="cnsr-candidates-server-$$-$RANDOM"
    start "$SERVER" "$WORKDIR/server" || fail "无法启动检查用的服务端容器。" 2
    CLIENT="cnsr-candidates-client-$$-$RANDOM"
    start "$CLIENT" "$WORKDIR/client" || fail "无法启动检查用的客户端容器。" 2
    # Xray opens every inbound before it says it started, so the last one stands for all.
    last=$((${#DOMAINS[@]} - 1))
    listening $((SERVER_BASE + last)) || fail "检查用的服务端在 $((WAIT_TRIES / 2)) 秒内没有开始监听。" 2
    listening $((SOCKS_BASE + last)) || fail "检查用的客户端在 $((WAIT_TRIES / 2)) 秒内没有开始监听。" 2

    for n in "${!DOMAINS[@]}"; do
        code=$(curl --socks5-hostname "127.0.0.1:$((SOCKS_BASE + n))" --max-time 10 -s -o /dev/null \
            -w '%{http_code}' "$PROBE_URL" 2>/dev/null || true)
        if [ "$code" = 204 ]; then
            printf '%s %s\n' "${DOMAINS[$n]}" "${IPS[$n]}"
        fi
    done
}

main "$@" </dev/null
exit 0
