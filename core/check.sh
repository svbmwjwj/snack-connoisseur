#!/bin/bash
# Snack Connoisseur - Health Check & Diagnostics Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ssh.sh" ]; then
    source "$LIB_DIR/ssh.sh"
fi
if [ -f "$SCRIPT_DIR/update.sh" ]; then
    source "$SCRIPT_DIR/update.sh"
fi

SSH_CONFIG_PATH="${TEST_SSH_CONFIG:-$HOME/.ssh/config}"

function module_check() {
    local alias="${1:-$SSH_ALIAS}"
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh check <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh check <别名>)"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🩺 Initiating full-link dual-perspective diagnostics: $alias"
    else
        echo "🩺 正在发起全链路双向诊断体检: $alias"
    fi

    sync_node_scripts "$alias"
    local sync_status=$?
    if [ $sync_status -ne 0 ]; then
        return $sync_status
    fi

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    local REMOTE_HOME=$(ssh "${ssh_opts[@]}" "$alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    # 1. 提取远端目标 IP 与当前配置的 SNI 伪装域名
    local NODE_INFO=$(ssh "${ssh_opts[@]}" "$alias" "
        sni=\$(jq -r '.inbounds[0].streamSettings.realitySettings.serverNames[0] // \"\"' ${DOCKER_APP_DIR}/conf/config.json 2>/dev/null || true)
        ip=\$(curl -4 -s -m 3 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print \$1}')
        echo \"\${ip}|\${sni}\"
    " 2>/dev/null || echo "|")

    local NODE_IP=$(echo "$NODE_INFO" | awk -F'|' '{print $1}')
    local NODE_SNI=$(echo "$NODE_INFO" | awk -F'|' '{print $2}')

    # 2. 本地端到端 TLS 握手测试 (模拟境内客户端穿透出境)
    local LOCAL_LINK_STATUS="UNKNOWN"
    if [ -n "$NODE_IP" ] && [ -n "$NODE_SNI" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "📡 [1/2] Testing local-to-remote TLS SNI handshake (Local 💻 -> GFW -> Node $alias)..."
        else
            echo "📡 [1/2] 测试本地出境 TLS 握手连通性 (本地 💻 -> GFW -> 节点 $alias)..."
        fi

        local probe_out=""
        probe_out=$((sleep 1; echo "") | openssl s_client -connect "${NODE_IP}:443" -servername "${NODE_SNI}" 2>&1 || true)
        
        if echo "$probe_out" | grep -q "Server certificate" && ! echo "$probe_out" | grep -q "no peer certificate available" && ! echo "$probe_out" | grep -q "Cipher is (NONE)"; then
            LOCAL_LINK_STATUS="OK"
            if [ "$CNSR_LANG" = "en" ]; then
                echo "   🟢 Local Handshake: ESTABLISHED (SNI [$NODE_SNI] is accessible domestically)"
            else
                echo "   🟢 本地出境握手: 正常建立 (伪装域名 [$NODE_SNI] 境内未被阻断)"
            fi
        else
            LOCAL_LINK_STATUS="BLOCKED"
            if [ "$CNSR_LANG" = "en" ]; then
                echo "   🔴 Local Handshake: FAILED (TLS connection reset/blocked, possible SNI/IP block!)"
            else
                echo "   🔴 本地出境握手: 握手失败/被重置 (疑似伪装域名 [$NODE_SNI] 或 IP 遭 GFW 阻断！)"
            fi
        fi
    fi

    # 3. 远端内部体检 (VPS 视角)
    if [ "$CNSR_LANG" = "en" ]; then
        echo "📡 [2/2] Calling remote node for internal health check..."
    else
        echo "📡 [2/2] 正在调用远端节点执行内部全面体检..."
    fi

    ssh "${ssh_opts[@]}" "$alias" "bash ${DOCKER_APP_DIR}/reality_check.sh --notify"
    local check_status=$?
    if [ $check_status -ne 0 ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Remote health check failed on [$alias] (Exit Code: $check_status)"
        else
            echo "❌ 节点 [$alias] 远端体检执行异常 (退出码: $check_status)"
        fi
        return $check_status
    fi

    # 4. 汇总双向诊断结果
    if [ "$LOCAL_LINK_STATUS" = "BLOCKED" ]; then
        echo ""
        echo "================================================================================"
        if [ "$CNSR_LANG" = "en" ]; then
            echo "⚠️ [ALERT] Zombie Node Detected: Remote services are healthy, but local TLS handshake failed!"
            echo "💡 Recommendation: Run './cnsr.sh rotate-sni $alias' to switch to a clean SNI domain."
        else
            echo "⚠️ 【告警】检测到节点处于【假死状态】：云端服务与出站正常，但本地入站握手被阻断！"
            echo "💡 建议处理：立即执行 './cnsr.sh rotate-sni $alias' 换用未被墙的干净域名。"
        fi
        echo "================================================================================"
        echo ""
        return 2
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Remote health check completed! Report dispatched via Telegram."
    else
        echo "✅ 全链路体检完成！报告已通过 Telegram 下发。"
    fi
    return 0
}
