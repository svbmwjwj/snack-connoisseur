#!/bin/bash
# Snack Connoisseur - Component Sync & Hot-Update Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ssh.sh" ]; then
    source "$LIB_DIR/ssh.sh"
fi
if [ -f "$LIB_DIR/security.sh" ]; then
    source "$LIB_DIR/security.sh"
fi

SSH_CONFIG_PATH="${TEST_SSH_CONFIG:-$HOME/.ssh/config}"

function sync_node_scripts() {
    local alias="$1"
    local override_ip="$2"
    local override_host="$3"
    
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: sync_node_scripts requires node alias."
        else
            echo "❌ 错误: sync_node_scripts 需要指定节点别名。"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🔄 Preparing to sync latest components and sanitized credentials for node [$alias]..."
    else
        echo "🔄 正在准备为节点 [$alias] 同步最新组件与脱敏凭据..."
    fi

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    # 1. 探测/解析远端用户信息与目录
    local REMOTE_HOME=$(ssh "${ssh_opts[@]}" "$alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    # 2. 探测 IP / IPv6 / CIDR / 伪装域名
    local IPV4="$override_ip"
    if [ -z "$IPV4" ]; then
        IPV4=$(ssh "${ssh_opts[@]}" "$alias" "curl -4 -s --connect-timeout 5 ifconfig.me || curl -4 -s --connect-timeout 5 api.ipify.org" 2>/dev/null || true)
    fi
    if [ -z "$IPV4" ]; then
        if declare -f get_real_host >/dev/null 2>&1; then
            IPV4=$(get_real_host "$alias")
        elif [ -f "$SSH_CONFIG_PATH" ]; then
            IPV4=$(ssh -F "$SSH_CONFIG_PATH" -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
        else
            IPV4=$(ssh -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
        fi
        [ -z "$IPV4" ] && IPV4="$alias"
    fi

    local IPV4_CIDR="none"
    if [[ "$IPV4" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        IFS=. read -r a b c d <<< "$IPV4"
        local C_BASE=$(( (c / 4) * 4 ))
        IPV4_CIDR="${a}.${b}.${C_BASE}.0/22"
    fi

    local IPV6=""
    if declare -f detect_remote_ipv6 >/dev/null 2>&1; then
        IPV6=$(detect_remote_ipv6 "$alias")
    fi
    local IPV6_CIDR="none"
    if [ "$IPV6" != "none" ] && [ -n "$IPV6" ]; then
        IPV6_CIDR=$(python3 -c "import sys, ipaddress; print(str(ipaddress.IPv6Network(f'{sys.argv[1]}/112', strict=False)))" "$IPV6" 2>/dev/null || echo "none")
        if [ -z "$IPV6_CIDR" ] || [ "$IPV6_CIDR" = "none" ]; then
            local V6_PREFIX=$(echo "$IPV6" | awk -F':' '{print $1":"$2":"$3":"$4}')
            [ -n "$V6_PREFIX" ] && IPV6_CIDR="${V6_PREFIX}::/64"
        fi
    fi

    # 获取真实/已有的伪装域名 (若传入 override_host 则优先使用，若远端已有则继承，否则读取 SSH HostName)
    local REAL_HOST="$override_host"
    if [ -z "$REAL_HOST" ]; then
        local REMOTE_SERVER_HOST=$(ssh "${ssh_opts[@]}" "$alias" "grep -E '^SERVER_HOST=' ${DOCKER_APP_DIR}/reality_rotate.sh 2>/dev/null | cut -d'\"' -f2" 2>/dev/null || true)
        if [ -n "$REMOTE_SERVER_HOST" ] && [ "$REMOTE_SERVER_HOST" != "PLACEHOLDER_HOST" ]; then
            REAL_HOST="$REMOTE_SERVER_HOST"
        else
            if declare -f get_real_host >/dev/null 2>&1; then
                REAL_HOST=$(get_real_host "$alias")
            else
                REAL_HOST="$alias"
            fi
            [ -z "$REAL_HOST" ] && REAL_HOST="$alias"
        fi
    fi

    # 3. 创建本地临时渲染目录
    local TMP_SYNC_DIR=$(mktemp -d)
    local REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

    # 3.1 渲染 runner.sh
    cp "$REPO_DIR/templates/runner.template.sh" "$TMP_SYNC_DIR/runner.sh"
    sed -i '' -e "s|TARGET_IP=\"PLACEHOLDER_IP\"|TARGET_IP=\"$IPV4\"|g" \
              -e "s|SERVER_HOST=\"PLACEHOLDER_HOST\"|SERVER_HOST=\"$REAL_HOST\"|g" \
              -e "s|SSH_ALIAS=\"PLACEHOLDER_ALIAS\"|SSH_ALIAS=\"$alias\"|g" \
              -e "s|TARGET_CIDR_V4=\"PLACEHOLDER_CIDR_V4\"|TARGET_CIDR_V4=\"$IPV4_CIDR\"|g" \
              -e "s|TARGET_CIDR_V6=\"PLACEHOLDER_CIDR_V6\"|TARGET_CIDR_V6=\"$IPV6_CIDR\"|g" \
              -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/runner.sh" 2>/dev/null || \
    sed -i -e "s|TARGET_IP=\"PLACEHOLDER_IP\"|TARGET_IP=\"$IPV4\"|g" \
           -e "s|SERVER_HOST=\"PLACEHOLDER_HOST\"|SERVER_HOST=\"$REAL_HOST\"|g" \
           -e "s|SSH_ALIAS=\"PLACEHOLDER_ALIAS\"|SSH_ALIAS=\"$alias\"|g" \
           -e "s|TARGET_CIDR_V4=\"PLACEHOLDER_CIDR_V4\"|TARGET_CIDR_V4=\"$IPV4_CIDR\"|g" \
           -e "s|TARGET_CIDR_V6=\"PLACEHOLDER_CIDR_V6\"|TARGET_CIDR_V6=\"$IPV6_CIDR\"|g" \
           -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/runner.sh"

    # 3.2 渲染 reality_rotate.sh
    cp "$REPO_DIR/templates/reality_rotate.template.sh" "$TMP_SYNC_DIR/reality_rotate.sh"
    sed -i '' -e "s|PLACEHOLDER_IP|$IPV4|g" \
              -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
              -e "s|PLACEHOLDER_ALIAS|$alias|g" \
              -e "s|PLACEHOLDER_CIDR_V4|$IPV4_CIDR|g" \
              -e "s|PLACEHOLDER_CIDR_V6|$IPV6_CIDR|g" \
              -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_rotate.sh" 2>/dev/null || \
    sed -i -e "s|PLACEHOLDER_IP|$IPV4|g" \
           -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
           -e "s|PLACEHOLDER_ALIAS|$alias|g" \
           -e "s|PLACEHOLDER_CIDR_V4|$IPV4_CIDR|g" \
           -e "s|PLACEHOLDER_CIDR_V6|$IPV6_CIDR|g" \
           -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_rotate.sh"

    # 3.3 渲染 reality_check.sh
    cp "$REPO_DIR/templates/reality_check.template.sh" "$TMP_SYNC_DIR/reality_check.sh"
    sed -i '' -e "s|PLACEHOLDER_IPV6|$IPV6|g" \
              -e "s|PLACEHOLDER_IP|$IPV4|g" \
              -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
              -e "s|PLACEHOLDER_ALIAS|$alias|g" \
              -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_check.sh" 2>/dev/null || \
    sed -i -e "s|PLACEHOLDER_IPV6|$IPV6|g" \
           -e "s|PLACEHOLDER_IP|$IPV4|g" \
           -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
           -e "s|PLACEHOLDER_ALIAS|$alias|g" \
           -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_check.sh"

    # 3.4 准备 tg_templates.sh
    cp "$REPO_DIR/templates/tg_templates.sh" "$TMP_SYNC_DIR/tg_templates.sh"

    # 3.5 准备脱敏 .env
    sanitize_env_for_node "$TMP_SYNC_DIR/.env"

    # 4. 本地静态语法校验 (bash -n)
    for script in runner.sh reality_rotate.sh reality_check.sh tg_templates.sh; do
        if ! bash -n "$TMP_SYNC_DIR/$script" >/dev/null 2>&1; then
            echo "❌ 严重错误: 生成的脚本 $script 本地语法检查 (bash -n) 失败！中断推送。"
            rm -rf "$TMP_SYNC_DIR"
            return 1
        fi
    done

    # 5. 上传至远端服务器
    local scp_opts=(-q -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        scp_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    ssh "${ssh_opts[@]}" "$alias" "mkdir -p ${DOCKER_APP_DIR}/conf"
    scp "${scp_opts[@]}" "$TMP_SYNC_DIR/runner.sh" "$alias":${DOCKER_APP_DIR}/runner.sh
    scp "${scp_opts[@]}" "$TMP_SYNC_DIR/reality_rotate.sh" "$alias":${DOCKER_APP_DIR}/reality_rotate.sh
    scp "${scp_opts[@]}" "$TMP_SYNC_DIR/reality_check.sh" "$alias":${DOCKER_APP_DIR}/reality_check.sh
    scp "${scp_opts[@]}" "$TMP_SYNC_DIR/tg_templates.sh" "$alias":${DOCKER_APP_DIR}/tg_templates.sh
    scp "${scp_opts[@]}" "$TMP_SYNC_DIR/.env" "$alias":${DOCKER_APP_DIR}/.env

    if [ -f "$REPO_DIR/tools/reality-checker" ]; then
        scp "${scp_opts[@]}" "$REPO_DIR/tools/reality-checker" "$alias":${DOCKER_APP_DIR}/reality-checker
    fi
    if [ -f "$REPO_DIR/fallback_snis.txt" ]; then
        scp "${scp_opts[@]}" "$REPO_DIR/fallback_snis.txt" "$alias":${DOCKER_APP_DIR}/fallback_snis.txt
    fi

    # 6. 赋予执行权限并挂载/刷新 Crontab 为 runner.sh
    ssh "${ssh_opts[@]}" "$alias" "
        chmod +x ${DOCKER_APP_DIR}/runner.sh ${DOCKER_APP_DIR}/reality_rotate.sh ${DOCKER_APP_DIR}/reality_check.sh ${DOCKER_APP_DIR}/tg_templates.sh 2>/dev/null || true
        [ -f ${DOCKER_APP_DIR}/reality-checker ] && chmod +x ${DOCKER_APP_DIR}/reality-checker 2>/dev/null || true
        if ! command -v crontab >/dev/null 2>&1; then if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -y && sudo apt-get install -y cron; fi; fi
        (sudo -u \$(whoami) crontab -l 2>/dev/null | grep -v -E 'reality_rotate.sh|runner.sh'; echo '*/15 * * * * ${DOCKER_APP_DIR}/runner.sh > ${DOCKER_APP_DIR}/rotate.log 2>&1') | sudo -u \$(whoami) crontab -
    "

    rm -rf "$TMP_SYNC_DIR"
    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Node [$alias] components and sanitized credentials synced!"
    else
        echo "✅ 节点 [$alias] 组件与脱敏凭据同步对齐完成！"
    fi
}

function module_update() {
    local alias="${1:-$SSH_ALIAS}"
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh update <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh update <别名>)"
        fi
        return 1
    fi

    sync_node_scripts "$alias"
    local sync_status=$?
    if [ $sync_status -ne 0 ]; then
        return $sync_status
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🎉 Node [$alias] components and sanitized credentials hot-updated successfully!"
    else
        echo "🎉 节点 [$alias] 全量组件与脱敏凭据热更新完成！"
    fi
}
