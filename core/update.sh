#!/bin/bash
# Snack Connoisseur - Component Sync & Hot-Update Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$LIB_DIR/ssh.sh" ]; then
    source "$LIB_DIR/ssh.sh"
fi
if [ -f "$LIB_DIR/security.sh" ]; then
    source "$LIB_DIR/security.sh"
fi

SSH_CONFIG_PATH="${SSH_CONFIG_PATH:-${TEST_SSH_CONFIG:-$HOME/.ssh/config}}"

function get_file_sha256() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo ""
        return
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" 2>/dev/null | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file" 2>/dev/null | awk '{print $1}'
    else
        uv run python -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" "$file" 2>/dev/null || echo ""
    fi
}

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
    local scp_opts=(-q -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [[ "$OSTYPE" != "msys" && "$OSTYPE" != "cygwin" ]]; then
        mkdir -p "$HOME/.ssh/sockets" 2>/dev/null || true
        ssh_opts+=(-o ControlMaster=auto -o ControlPath="$HOME/.ssh/sockets/%C" -o ControlPersist=5m)
        scp_opts+=(-o ControlMaster=auto -o ControlPath="$HOME/.ssh/sockets/%C" -o ControlPersist=5m)
    else
        ssh_opts+=(-o ControlMaster=no -o ControlPath=none)
        scp_opts+=(-o ControlMaster=no -o ControlPath=none)
    fi
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
        scp_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    # 1. 单次探测/解析远端用户信息、目录、公网 IPv4/IPv6、伪装域名与资产哈希指纹表 (远端唯一事实源)
    local PROBE_INFO=$(ssh "${ssh_opts[@]}" "$alias" "
        user=\$(eval echo ~\$USER)
        docker_dir=\"\${user}/docker-apps/xray\"
        ip=\$(curl -4 -s --connect-timeout 3 ifconfig.me 2>/dev/null || curl -4 -s --connect-timeout 3 icanhazip.com 2>/dev/null || curl -4 -s --connect-timeout 3 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n1 || true)
        ipv6=\$(hostname -I 2>/dev/null | tr ' ' '\n' | grep ':' | grep -v '^fe80' | grep -v '^fc' | grep -v '^fd' | head -n1 || curl -6 -s --connect-timeout 3 icanhazip.com 2>/dev/null || curl -6 -s --connect-timeout 3 ifconfig.me 2>/dev/null || true)
        server_host=\$(grep -E '^SERVER_HOST=' \${docker_dir}/reality_rotate.sh 2>/dev/null | cut -d'\"' -f2 || true)
        mkdir -p \${docker_dir}/conf 2>/dev/null || true
        hashes=\"\"
        for f in runner.sh reality_rotate.sh reality_check.sh tg_templates.sh .env reality-checker fallback_snis.txt; do
            if [ -f \"\${docker_dir}/\$f\" ]; then
                h=\$(sha256sum \"\${docker_dir}/\$f\" 2>/dev/null | awk '{print \$1}')
                [ -n \"\$h\" ] && hashes=\"\${hashes}\${f}:\${h},\"
            fi
        done
        echo \"\${user}|\${ip}|\${ipv6}|\${server_host}|\${hashes}\"
    " 2>/dev/null || echo "/home/admin||||")

    local REMOTE_HOME=$(echo "$PROBE_INFO" | awk -F'|' '{print $1}')
    [ -z "$REMOTE_HOME" ] && REMOTE_HOME="/home/admin"
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    local PROBED_IP=$(echo "$PROBE_INFO" | awk -F'|' '{print $2}')
    local PROBED_IPV6=$(echo "$PROBE_INFO" | awk -F'|' '{print $3}')
    local REMOTE_SERVER_HOST=$(echo "$PROBE_INFO" | awk -F'|' '{print $4}')
    local REMOTE_HASHES=$(echo "$PROBE_INFO" | awk -F'|' '{print $5}')

    # 2. 确定 IP / IPv6 / CIDR / 伪装域名 (远端探针为唯一权威物理来源)
    local IPV4="$override_ip"
    [ -z "$IPV4" ] && IPV4="$PROBED_IP"

    local IPV4_CIDR="none"
    if [[ "$IPV4" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        IFS=. read -r a b c d <<< "$IPV4"
        local C_BASE=$(( (c / 4) * 4 ))
        IPV4_CIDR="${a}.${b}.${C_BASE}.0/22"
    fi

    local IPV6="$PROBED_IPV6"
    if [ -z "$IPV6" ] && declare -f detect_remote_ipv6 >/dev/null 2>&1; then
        IPV6=$(detect_remote_ipv6 "$alias")
    fi
    local IPV6_CIDR="none"
    if [ "$IPV6" != "none" ] && [ -n "$IPV6" ]; then
        IPV6_CIDR=$(cd "$REPO_DIR" && uv run python -c "import sys, ipaddress; print(str(ipaddress.IPv6Network(f'{sys.argv[1]}/112', strict=False)))" "$IPV6" 2>/dev/null || echo "none")
        if [ -z "$IPV6_CIDR" ] || [ "$IPV6_CIDR" = "none" ]; then
            local V6_PREFIX=$(echo "$IPV6" | awk -F':' '{print $1":"$2":"$3":"$4}')
            [ -n "$V6_PREFIX" ] && IPV6_CIDR="${V6_PREFIX}::/64"
        fi
    fi

    # 获取真实/已有的伪装域名 (若传入 override_host 则优先使用，若远端已有则继承，否则读取 SSH HostName)
    local REAL_HOST="$override_host"
    if [ -z "$REAL_HOST" ]; then
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

    # 5. 基于 SHA-256 哈希指纹进行差异比对
    local candidate_files=(
        "runner.sh:$TMP_SYNC_DIR/runner.sh"
        "reality_rotate.sh:$TMP_SYNC_DIR/reality_rotate.sh"
        "reality_check.sh:$TMP_SYNC_DIR/reality_check.sh"
        "tg_templates.sh:$TMP_SYNC_DIR/tg_templates.sh"
        ".env:$TMP_SYNC_DIR/.env"
    )
    if [ -f "$REPO_DIR/tools/reality-checker" ]; then
        candidate_files+=("reality-checker:$REPO_DIR/tools/reality-checker")
    fi
    if [ -f "$REPO_DIR/fallback_snis.txt" ]; then
        candidate_files+=("fallback_snis.txt:$REPO_DIR/fallback_snis.txt")
    fi

    local files_to_sync=()
    local updated_names=()

    for item in "${candidate_files[@]}"; do
        local fname="${item%%:*}"
        local fpath="${item#*:}"
        local local_hash=$(get_file_sha256 "$fpath")
        local remote_hash=$(echo "$REMOTE_HASHES" | tr ',' '\n' | grep "^${fname}:" | cut -d':' -f2 || true)

        if [ -n "$local_hash" ] && [ "$local_hash" != "$remote_hash" ]; then
            files_to_sync+=("$fpath")
            updated_names+=("$fname")
        fi
    done

    # 6. 按需执行增量传输与权限/Cron 配置
    if [ ${#files_to_sync[@]} -gt 0 ]; then
        scp "${scp_opts[@]}" "${files_to_sync[@]}" "$alias":${DOCKER_APP_DIR}/

        ssh "${ssh_opts[@]}" "$alias" "
            chmod +x ${DOCKER_APP_DIR}/runner.sh ${DOCKER_APP_DIR}/reality_rotate.sh ${DOCKER_APP_DIR}/reality_check.sh ${DOCKER_APP_DIR}/tg_templates.sh 2>/dev/null || true
            [ -f ${DOCKER_APP_DIR}/reality-checker ] && chmod +x ${DOCKER_APP_DIR}/reality-checker 2>/dev/null || true
            if ! command -v crontab >/dev/null 2>&1; then if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -y && sudo apt-get install -y cron; fi; fi
            (crontab -l 2>/dev/null | grep -v -E 'reality_rotate.sh|runner.sh'; echo '*/15 * * * * ${DOCKER_APP_DIR}/runner.sh > ${DOCKER_APP_DIR}/rotate.log 2>&1') | crontab -
        "
        if [ "$CNSR_LANG" = "en" ]; then
            echo "✅ Node [$alias] components updated (${#files_to_sync[@]} items changed: ${updated_names[*]})"
        else
            echo "✅ 节点 [$alias] 组件增量对齐完成 (更新 ${#files_to_sync[@]} 项: ${updated_names[*]})"
        fi
    else
        if [ "$CNSR_LANG" = "en" ]; then
            echo "✅ Node [$alias] components and credentials are up to date (0 items synced)"
        else
            echo "✅ 节点 [$alias] 组件与脱敏凭据均已是最新状态 (哈希指纹完全匹配，0 传输)"
        fi
    fi

    rm -rf "$TMP_SYNC_DIR"
}

function module_update() {
    local first_arg="${1:-$SSH_ALIAS}"
    if [ $# -eq 0 ] || [ -z "$first_arg" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh update <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh update <别名>)"
        fi
        return 1
    fi

    local target_aliases=()
    local input_args=("$@")
    [ ${#input_args[@]} -eq 0 ] && input_args=("$SSH_ALIAS")

    for arg in "${input_args[@]}"; do
        if [[ "$arg" == *"*"* ]]; then
            local base_pattern="${arg%\*}"
            while IFS= read -r line; do
                if [[ "$line" == Host\ $base_pattern* ]]; then
                    local found_alias=$(echo "$line" | awk '{print $2}')
                    target_aliases+=("$found_alias")
                fi
            done < <(grep -i "^Host " "$SSH_CONFIG_PATH" 2>/dev/null || true)
        else
            target_aliases+=("$arg")
        fi
    done

    if [ ${#target_aliases[@]} -gt 0 ]; then
        IFS=$'\n' target_aliases=($(sort -V -u <<<"${target_aliases[*]}"))
        unset IFS
    fi

    if [ ${#target_aliases[@]} -eq 0 ]; then
        echo "❌ 未找到任何有效的目标节点。"
        return 1
    fi

    local has_err=0
    for cur_alias in "${target_aliases[@]}"; do
        if ! sync_node_scripts "$cur_alias"; then
            has_err=1
        else
            if [ "$CNSR_LANG" = "en" ]; then
                echo "🎉 Node [$cur_alias] components and sanitized credentials hot-updated successfully!"
            else
                echo "🎉 节点 [$cur_alias] 全量组件与脱敏凭据热更新完成！"
            fi
        fi
    done
    return $has_err
}
