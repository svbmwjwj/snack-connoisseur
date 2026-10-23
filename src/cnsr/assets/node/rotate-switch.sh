#!/usr/bin/env bash
# rotate-switch.sh: cnsr-rotate.sh's switch to a backup: checks, the Xray config swap, rollback
# and recovery of a switch cut short.
# Sourced by cnsr-rotate.sh, whose constants, state and log/fail it uses; it only defines
# functions. Must parse under bash 3.2.

# replace_file <file> <jq filter> [jq options...]: <file> run through jq, replaced in one step.
replace_file() {
    local file=$1 filter=$2
    shift 2
    jq "$@" "$filter" "$file" >"$file.tmp" || { rm -f "$file.tmp"; return 1; }
    mv -f "$file.tmp" "$file"
}

restart_xray() {
    docker compose -f "$COMPOSE" restart </dev/null >&2
}

# Put the config from before the switch back and restart Xray with it.
roll_back() {
    cp -p "$PREV" "$CONFIG.tmp" && mv -f "$CONFIG.tmp" "$CONFIG" && restart_xray
}

# pending_line <domain> <ip>: the text of line-pending.json for this backup.
pending_line() {
    printf '{"sni":"%s","dest_ip":"%s"}' "$1" "$2"
}

# Undo a switch that was cut short (SWITCH_MARK holds its pending line, then its backup entry):
# config.prev.json goes back, Xray restarts and the backup returns to the head of the list. A
# switch whose line already reached line-pending.json was complete. Fails when it cannot.
recover_switch() {
    local line="" entry=""
    [ -f "$SWITCH_MARK" ] || return 0
    { IFS= read -r line && IFS= read -r entry; } <"$SWITCH_MARK" || true
    if [ -f "$PENDING" ] && [ "$(cat "$PENDING")" = "$line" ]; then
        rm -f "$SWITCH_MARK"
        return 0
    fi
    log "上次换 SNI 没有完成，恢复换之前的 Xray 配置。"
    if [ ! -f "$PREV" ] || ! command -v docker >/dev/null 2>&1 || ! roll_back; then
        log "恢复换之前的 Xray 配置失败，下一轮再试。"
        return 1
    fi
    if [ -n "$entry" ] && command -v jq >/dev/null 2>&1; then
        replace_file "$BACKUPS" '[$entry] + .' --argjson entry "$entry" || log "无法把备选放回列表：$entry"
    fi
    rm -f "$SWITCH_MARK"
}

# TERM, INT or HUP: never leave Xray on a config whose handshake was not checked. Exits 143
# (128 + TERM), the one status cnsr-rotate.service counts as a clean stop. A check never
# switches: a switch in progress is the running service's, so it is left alone.
on_signal() {
    local put_back=""
    trap - HUP INT TERM
    # Decided before recover_switch, which puts back the entry of a switch it undoes itself.
    [ -n "$TRYING" ] && [ ! -f "$SWITCH_MARK" ] && put_back=$TRYING
    [ -n "$CHECKING" ] || recover_switch || true
    if [ -n "$put_back" ]; then
        # TRYING is set before the entry leaves the list, so it may still be at the head.
        replace_file "$BACKUPS" 'if .[0] == $entry then . else [$entry] + . end' --argjson entry "$put_back" ||
            log "无法把备选放回列表：$put_back"
    fi
    exit 143
}

# node_online: 0 when the node reaches the internet itself.
node_online() {
    [ "$(curl -s -o /dev/null --max-time 8 -w '%{http_code}' "$ONLINE_URL" </dev/null)" = 204 ]
}

# self_check <domain>: the REALITY handshake from the node with the new SNI; 1 when it failed,
# 2 when the check itself could not run. The values go to verify-reality.sh in a root-only
# file, which it deletes.
self_check() {
    local values
    values=$(mktemp "$ROOT/.verify.XXXXXX") || return 1
    printf 'UUID=%s\nPUBLIC_KEY=%s\nSHORT_ID=%s\nSNI=%s\n' "$UUID" "$PUBLIC_KEY" "$SHORT_ID" "$1" >"$values"
    bash "$BIN/verify-reality.sh" "$values" </dev/null >&2
}

# try_backup <domain> <ip> <entry>: 0 when Xray now runs with it, passed the self-check and
# its line is in line-pending.json; 1 when it is no good (the old config is back); 2 when
# something else failed (FAILURE says what; the old config is back unless SWITCH_MARK is left
# for recover_switch).
# china_ok <domain> <ip>: 1 when mainland probes find the backup blocked; a missing check (a
# node set up before it) or one with no answer lets the backup through.
china_ok() {
    local verdict=unknown left=$((CHINA_SINCE + CHINA_BUDGET_SECONDS - $(date +%s)))
    if [ "$left" -le 0 ]; then
        log "这次换 SNI 的国内检验已用完 ${CHINA_BUDGET_SECONDS} 秒，备选 $1 只凭本机验证。"
        return 0
    fi
    if [ -f "$BIN/china-check.sh" ]; then
        verdict=$(bash "$BIN/china-check.sh" "$1" "$2" "$left" </dev/null) || verdict=unknown
    fi
    case "$verdict" in
        pass) return 0 ;;
        blocked)
            log "备选 $1（$2）从国内握手失败（Globalping 大陆探针），多半按 SNI 被墙，丢掉。"
            return 1
            ;;
        *)
            log "没能从国内检验备选 $1，只凭本机验证。"
            return 0
            ;;
    esac
}

try_backup() {
    local domain=$1 ip=$2 entry=$3 passed
    passed=$(bash "$BIN/validate-candidates.sh" "$domain" "$ip" </dev/null) || {
        FAILURE="本机验证备选时出错"
        return 2
    }
    case $'\n'"$passed"$'\n' in
        *$'\n'"$domain $ip "*) ;;
        *)
            node_online || { FAILURE=$OFFLINE_FAILURE; return 2; }
            log "备选 $domain（$ip）没有通过本机验证，丢掉。"
            return 1
            ;;
    esac
    china_ok "$domain" "$ip" || return 1
    if ! jq --arg dest "$ip:443" --arg name "$domain" \
        '.inbounds[0].streamSettings.realitySettings |= (.dest = $dest | .serverNames = [$name])' \
        "$CONFIG" >"$STAGED" || ! chmod 644 "$STAGED"; then
        rm -f "$STAGED"
        FAILURE="无法生成新的 Xray 配置"
        return 2
    fi
    if ! docker run --rm -v "$CONF_DIR:$CONTAINER_CONF_DIR:ro" "$XRAY_IMAGE" \
        run -test -c "$CONTAINER_CONF_DIR/config.new.json" </dev/null >/dev/null; then
        rm -f "$STAGED"
        FAILURE="新的 Xray 配置没有通过 xray -test"
        return 2
    fi
    # The previous config is saved before the mark, so the mark never points at a stale one.
    if ! cp -p "$CONFIG" "$PREV" || ! printf '%s\n%s\n' "$(pending_line "$domain" "$ip")" "$entry" >"$SWITCH_MARK.tmp" ||
        ! mv -f "$SWITCH_MARK.tmp" "$SWITCH_MARK"; then
        rm -f "$STAGED" "$SWITCH_MARK.tmp"
        FAILURE="无法保存旧的 Xray 配置"
        return 2
    fi
    if ! mv -f "$STAGED" "$CONFIG"; then
        rm -f "$STAGED" "$SWITCH_MARK"
        FAILURE="无法换上新的 Xray 配置"
        return 2
    fi
    if ! restart_xray; then
        undo_switch "重启 Xray 失败"
        return 2
    fi
    local checked=0
    self_check "$domain" || checked=$?
    if [ "$checked" -eq 2 ]; then
        undo_switch "REALITY 自检程序没能运行"
        return 2
    elif [ "$checked" -ne 0 ]; then
        log "备选 $domain（$ip）没有通过 REALITY 自检，恢复旧配置。"
        undo_switch "REALITY 自检没有通过" || return 2
        node_online || { FAILURE=$OFFLINE_FAILURE; return 2; }
        return 1
    fi
    if ! { pending_line "$domain" "$ip" >"$PENDING.tmp" && printf '\n' >>"$PENDING.tmp" && mv -f "$PENDING.tmp" "$PENDING"; }; then
        rm -f "$PENDING.tmp"
        undo_switch "无法记下新 SNI" || return 2
        FAILURE="无法记下新 SNI，已恢复旧配置"
        return 2
    fi
    TRYING=""  # first: a stop must not put back the SNI now in use
    rm -f "$SWITCH_MARK"
    return 0
}

# undo_switch <why>: roll back and clear SWITCH_MARK; when the rollback fails, FAILURE says so,
# the mark stays for recover_switch and it fails.
undo_switch() {
    if roll_back; then
        rm -f "$SWITCH_MARK"
        FAILURE="$1，已恢复旧配置"
        return 0
    fi
    FAILURE="$1，恢复旧配置后重启 Xray 也失败了"
    return 1
}

# Take backups off the list until one works, or the list is empty.
rotate() {
    local tool missing="" entry domain ip result tried
    for tool in jq docker; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    if [ -n "$missing" ]; then
        report_failed "节点上缺少${missing}，没有换 SNI"
        return 0
    fi
    if [ ! -f "$CONFIG" ] || [ ! -f "$BIN/validate-candidates.sh" ] || [ ! -f "$BIN/verify-reality.sh" ]; then
        report_failed "节点上缺少 Xray 配置或检测脚本，没有换 SNI"
        return 0
    fi
    if ! jq -e 'type == "array"' "$BACKUPS" >/dev/null 2>&1; then
        report_failed "备选 SNI 列表无法读取，没有换 SNI"
        return 0
    fi
    CHINA_SINCE=$(date +%s)
    tried=0
    while [ "$(jq 'length' "$BACKUPS")" -gt 0 ]; do
        tried=$((tried + 1))
        entry=$(jq -c '.[0]' "$BACKUPS")
        domain=$(jq -r '.[0] | objects | .domain | strings' "$BACKUPS")
        ip=$(jq -r '.[0] | objects | .ip | strings' "$BACKUPS")
        TRYING=$entry
        replace_file "$BACKUPS" '.[1:]' || fail "无法写回备选 SNI 列表。"
        if ! [[ $domain =~ $DOMAIN_PATTERN && $ip =~ $IP_PATTERN ]]; then
            TRYING=""
            log "跳过无法识别的备选：$entry"
            continue
        fi
        result=0
        try_backup "$domain" "$ip" "$entry" || result=$?
        TRYING=""
        case "$result" in
            0)
                rm -f "$FAILED_MARK" "$LINE_RETRY_MARK"
                log "已换到 $domain（$ip），REALITY 自检通过。"
                flush_pending || true
                return 0
                ;;
            1) ;;
            *)
                # Not this backup's fault: it goes back to the head of the list (recover_switch
                # puts it back when the mark is still there).
                if [ ! -f "$SWITCH_MARK" ]; then
                    replace_file "$BACKUPS" '[$entry] + .' --argjson entry "$entry" || fail "无法写回备选 SNI 列表。"
                fi
                report_failed "$FAILURE"
                return 0
                ;;
        esac
    done
    if [ "$tried" -eq 0 ]; then
        log "备选 SNI 已用完，保持旧配置。"
        event exhausted "备选 SNI 已用完，保持旧配置"
    else
        log "备选 SNI 都没通过复验，保持旧配置。"
        event exhausted "备选 SNI 都没通过复验，保持旧配置"
    fi
}
