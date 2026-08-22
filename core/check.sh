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
        echo "🩺 Calling remote node for comprehensive health check: $alias"
    else
        echo "🩺 正在呼叫远端节点执行全面体检: $alias"
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

    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Remote health check completed! Report dispatched via Telegram."
    else
        echo "✅ 远端体检完成！报告已通过 Telegram 下发。"
    fi
    return 0
}
