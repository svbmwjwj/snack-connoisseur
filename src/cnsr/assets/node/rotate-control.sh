#!/usr/bin/env bash
# rotate-control.sh: cnsr-rotate.sh's side of the control plane: HTTP calls, the 401 spell,
# versions, events and writing back a pending line.
# Sourced by cnsr-rotate.sh, whose constants, state and log/fail it uses; it only defines
# functions. Must parse under bash 3.2.

# The control plane rejected the token: the node was removed, so stop for good.
stop() {
    : >"$ROOT/stopped"
    rm -f "$UNAUTHORIZED_MARK"
    log "控制端拒绝了节点 token（节点可能已被删除），换 SNI 服务停止。"
    exit 0
}

# A 401: the first one starts UNAUTHORIZED_MARK; one more than UNAUTHORIZED_STOP_SECONDS later
# stops the service.
refused() {
    local since=""
    if [ -f "$UNAUTHORIZED_MARK" ]; then
        since=$(cat "$UNAUTHORIZED_MARK")
    fi
    case "$since" in
        '' | *[!0-9]*)
            date +%s >"$UNAUTHORIZED_MARK"
            log "控制端拒绝了节点 token，${UNAUTHORIZED_STOP_SECONDS} 秒后仍被拒绝就停止换 SNI 服务。"
            return 0
            ;;
    esac
    if [ $(($(date +%s) - since)) -gt "$UNAUTHORIZED_STOP_SECONDS" ]; then
        stop
    fi
}

# version_in <text>: the number in its "version" field, or nothing.
version_in() {
    if [[ "$1 " =~ $VERSION_PATTERN ]]; then
        printf '%s' "$((10#${BASH_REMATCH[1]}))"
    fi
}

# The version of the last line the control plane accepted, or nothing.
last_version() {
    local value=""
    if [ -f "$LAST_VERSION" ]; then
        value=$(cat "$LAST_VERSION")
    fi
    case "$value" in
        '' | *[!0-9]* | 0*) ;;
        *) printf '%s' "$value" ;;
    esac
}

# http <method> <endpoint> [<json body>]: HTTP_STATUS ("000" when the control plane was not
# reached) and HTTP_BODY. 401s are counted by refused(); any other answer ends their spell.
http() {
    local args=(-sS -K - -A "$USER_AGENT" --max-time "$TIMEOUT_SECONDS" -o "$RESPONSE" -w '%{http_code}' -X "$1")
    if [ "$#" -ge 3 ]; then
        args+=(-H 'Content-Type: application/json' --data-binary "$3")
    fi
    rm -f "$RESPONSE"
    HTTP_STATUS=$(printf 'header = "Authorization: Bearer %s"\n' "$NODE_TOKEN" | curl "${args[@]}" "$CONTROL/node/$ALIAS/$2") || true
    HTTP_STATUS=${HTTP_STATUS:-000}
    HTTP_BODY=""
    if [ -f "$RESPONSE" ]; then
        HTTP_BODY=$(cat "$RESPONSE")
        rm -f "$RESPONSE"
    fi
    [ -z "$CHECKING" ] || return 0
    case "$HTTP_STATUS" in
        401) refused ;;
        000) ;;
        *) rm -f "$UNAUTHORIZED_MARK" ;;
    esac
}

# event <kind> <detail>: the detail is one of this script's own texts, plus names that passed
# DOMAIN_PATTERN, so it needs no JSON escaping.
event() {
    http POST event "{\"kind\":\"$1\",\"detail\":\"$2\"}"
    if [ "$HTTP_STATUS" != 204 ]; then
        log "向控制端报告 $1 失败（HTTP $HTTP_STATUS）。"
    fi
}

report_failed() {
    log "换 SNI 失败：$1"
    date +%s >"$FAILED_MARK"
    event rotate_failed "$1"
}

# backing_off <mark> <seconds>: whether the time in <mark> is less than <seconds> ago.
backing_off() {
    local since
    [ -f "$1" ] || return 1
    since=$(cat "$1")
    case "$since" in
        '' | *[!0-9]*) return 1 ;;
    esac
    [ $(($(date +%s) - since)) -lt "$2" ]
}

# Write the line in line-pending.json to the control plane; fails while it is still pending.
# A control plane that cannot be reached or fails (000, 5xx, 408, 429) is tried again after
# LINE_RETRY_SECONDS; a line it refuses is dropped, as asking again cannot change the answer.
# A 401 is retried too: it may be the control plane's cache, and enough of them stop the service.
# The version it answers goes in LAST_VERSION.
flush_pending() {
    local version last
    [ -f "$PENDING" ] || return 0
    if backing_off "$LINE_RETRY_MARK" "$LINE_RETRY_SECONDS"; then
        return 1
    fi
    last=$(last_version)
    http PUT "line${last:+?version=$last}" "$(cat "$PENDING")"
    case "$HTTP_STATUS" in
        200)
            version=$(version_in "$HTTP_BODY")
            if [ -n "$version" ]; then
                printf '%s\n' "$version" >"$LAST_VERSION.tmp" && mv -f "$LAST_VERSION.tmp" "$LAST_VERSION" ||
                    log "无法记下控制端的版本 $version。"
            fi
            rm -f "$PENDING" "$LINE_RETRY_MARK"
            log "新 SNI 已写回控制端。"
            return 0
            ;;
        401 | 408 | 429) ;;
        4[0-9][0-9])
            rm -f "$PENDING" "$LINE_RETRY_MARK"
            report_failed "控制端拒绝了新 SNI（HTTP $HTTP_STATUS），节点已在用新 SNI"
            return 1
            ;;
    esac
    date +%s >"$LINE_RETRY_MARK"
    log "新 SNI 还没写回控制端（HTTP $HTTP_STATUS），$LINE_RETRY_SECONDS 秒后再试。"
    return 1
}
