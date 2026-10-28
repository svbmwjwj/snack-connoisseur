#!/usr/bin/env bash
# cnsr-rotate.sh [once|check]
#
# The node's SNI rotation service, run by systemd from /opt/cnsr/bin (cnsr-rotate.service).
# Every POLL_SECONDS it asks the control plane whether this node should change its SNI; with
# "once" it does a single round and exits, without it it polls until the control plane has
# answered 401 for more than UNAUTHORIZED_STOP_SECONDS (the node was removed; KV can serve an
# old record for a moment), then writes /opt/cnsr/stopped and exits 0. With "check" it asks
# for the task once, without ?version, and prints the HTTP status ("000": not reached), for
# cnsr up to see that the node reaches the control plane; it changes nothing on the node.
#
# One round: a new line that could not be written to the control plane last time
# (line-pending.json) is written first, and nothing else happens until that works. Then
# GET task; on {"rotate": true} the next backup from backups.json is taken off the list,
# validated from this node, then from mainland China (china-check.sh: a backup it finds
# blocked is dropped; one it cannot judge goes by this node alone), put into the Xray config (only REALITY's dest and serverNames
# change), checked with `xray run -test`, Xray restarted and the REALITY handshake checked
# from the node. A backup that does not validate is dropped; one that fails the handshake is
# rolled back and dropped; the next one is tried. The first that passes is written back with
# PUT line. No backup left: POST event exhausted, old config kept. A missing tool or a failing
# docker, validation or restart: POST event rotate_failed, old config and the backup kept, and
# no new attempt for RETRY_SECONDS (each event is a KV write on the control plane). A line the
# control plane could not take is retried after LINE_RETRY_SECONDS; one it refuses (4xx) is
# dropped and reported as rotate_failed. A switch cut short (`switching` exists, its line is
# not in line-pending.json) is undone on TERM, INT or HUP, or at the start of the next round.
#
# The control plane reads its state through a cache that can lag behind its own writes. The
# version an accepted PUT line answers goes in last-version and is sent with every GET task
# and PUT line (?version=N), so the control plane can repair a state that fell behind and never
# hands out a version twice; a task whose version is older than last-version is about the
# config before the last switch and is ignored. Any answer but a 401 or none ends a 401 spell.
#
# The node token is passed to curl in a config on its stdin, never on a command line, and is
# never logged. env files are read as text, never sourced. It never exits 255 and must parse
# under bash 3.2. CNSR_ROOT replaces /opt/cnsr for tests only.

set -euo pipefail
trap 'status=$?; if [ "$status" = 255 ]; then exit 1; fi' EXIT
trap on_signal HUP INT TERM
export LC_ALL=C
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
umask 077

ROOT=${CNSR_ROOT:-/opt/cnsr}
BIN=$ROOT/bin
BACKUPS=$ROOT/backups.json
PENDING=$ROOT/line-pending.json
FAILED_MARK=$ROOT/rotate-failed
LINE_RETRY_MARK=$ROOT/line-retry
SWITCH_MARK=$ROOT/switching
LAST_VERSION=$ROOT/last-version
UNAUTHORIZED_MARK=$ROOT/unauthorized-since
RESPONSE=$ROOT/.http-response
CONF_DIR=$ROOT/xray/conf
CONFIG=$CONF_DIR/config.json
# Xray tells a config file's format by its extension, so the staged copy must end in .json.
STAGED=$CONF_DIR/config.new.json
PREV=$CONF_DIR/config.prev.json
COMPOSE=$ROOT/xray/compose.yml
# The image of the server (cnsr.server.xray.XRAY_IMAGE; a test keeps the two equal).
XRAY_IMAGE=ghcr.io/xtls/xray-core:26.3.27
CONTAINER_CONF_DIR=/usr/local/etc/xray

POLL_SECONDS=15
RETRY_SECONDS=3600
LINE_RETRY_SECONDS=300
TIMEOUT_SECONDS=10
UNAUTHORIZED_STOP_SECONDS=120
USER_AGENT=cnsr-node/1
# Answers 204 to anyone online: a backup that fails while this fails too is not to blame.
ONLINE_URL=https://www.gstatic.com/generate_204
OFFLINE_FAILURE="节点自己连不上外网，没有换 SNI"

DOMAIN_PATTERN='^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$'
IP_PATTERN='^[0-9A-Fa-f:.]+$'
TOKEN_PATTERN='^[A-Za-z0-9_-]+$'
CONTROL_PATTERN='^https://[A-Za-z0-9.-]+(:[0-9]+)?$'
ROTATE_PATTERN='"rotate"[[:space:]]*:[[:space:]]*true'
VERSION_PATTERN='"version"[[:space:]]*:[[:space:]]*([0-9]{1,15})[^0-9]'

CONTROL="" NODE_TOKEN="" ALIAS="" UUID="" PUBLIC_KEY="" SHORT_ID=""
HTTP_STATUS="" HTTP_BODY=""
CHECKING=""
# The backup taken off the list and not yet decided on: a stop before its switch puts it back.
TRYING=""
# All mainland checks of one rotation share this many seconds; after that the node decides alone.
CHINA_BUDGET_SECONDS=${CNSR_CHINA_BUDGET_SECONDS:-90}
CHINA_SINCE=0
FAILURE=""

# The control-plane calls and the switch itself live next to this script.
. "$BIN/rotate-control.sh"
. "$BIN/rotate-switch.sh"

log() {
    printf '%s\n' "$1" >&2
}

fail() {
    log "$1"
    exit 1
}

# read_env <file>: the KEY=value lines this service uses; any other line is ignored.
read_env() {
    local line key value
    [ -f "$1" ] || fail "找不到 $1。"
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        key=${line%%=*}
        value=${line#*=}
        case "$key" in
            CNSR_CONTROL) CONTROL=${value%/} ;;
            CNSR_NODE_TOKEN) NODE_TOKEN=$value ;;
            ALIAS) ALIAS=$value ;;
            UUID) UUID=$value ;;
            PUBLIC_KEY) PUBLIC_KEY=$value ;;
            SHORT_ID) SHORT_ID=$value ;;
        esac
    done <"$1"
}


tick() {
    local last task_version
    recover_switch || return 0
    flush_pending || return 0
    last=$(last_version)
    http GET "task${last:+?version=$last}"
    if [ "$HTTP_STATUS" != 200 ]; then
        log "查询换 SNI 任务失败（HTTP $HTTP_STATUS）。"
        return 0
    fi
    [[ $HTTP_BODY =~ $ROTATE_PATTERN ]] || return 0
    task_version=$(version_in "$HTTP_BODY")
    if [ -n "$last" ] && [ -n "$task_version" ] && [ "$task_version" -lt "$last" ]; then
        log "控制端的换 SNI 任务是第 $task_version 版的，本机上次写回的是第 $last 版，忽略。"
        return 0
    fi
    if backing_off "$FAILED_MARK" "$RETRY_SECONDS"; then
        return 0
    fi
    log "控制端要求换 SNI。"
    rotate
}

# The running service shares nothing with a check but the env files: its own response file,
# and no 401 bookkeeping.
check() {
    RESPONSE=$ROOT/.http-response-check
    http GET task
    printf '%s\n' "$HTTP_STATUS"
}

main() {
    case "${1-}" in
        '' | once) ;;
        check) CHECKING=1 ;;
        *) fail "用法：cnsr-rotate.sh [once|check]" ;;
    esac
    read_env "$ROOT/node.env"
    read_env "$ROOT/identity.env"
    [[ $CONTROL =~ $CONTROL_PATTERN ]] || fail "node.env 里的控制端地址格式不对。"
    [[ $NODE_TOKEN =~ $TOKEN_PATTERN ]] || fail "node.env 里的节点 token 格式不对。"
    [[ $ALIAS =~ $DOMAIN_PATTERN ]] || fail "identity.env 里的别名格式不对。"
    command -v curl >/dev/null 2>&1 || fail "节点上没有 curl，无法连接控制端。"
    if [ "${1-}" = check ]; then
        check
        return 0
    fi
    if [ "${1-}" = once ]; then
        tick
        return 0
    fi
    while :; do
        tick
        sleep "$POLL_SECONDS"
    done
}

main "$@" </dev/null
exit 0
