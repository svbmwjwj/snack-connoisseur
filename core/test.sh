#!/bin/bash
# Snack Connoisseur - Testing & Preview Suite Module
# Part of core/ operations suite (test-tg and test-sni)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ssh.sh" ]; then
    source "$LIB_DIR/ssh.sh"
fi
if [ -f "$SCRIPT_DIR/update.sh" ]; then
    source "$SCRIPT_DIR/update.sh"
fi

SSH_CONFIG_PATH="${TEST_SSH_CONFIG:-$HOME/.ssh/config}"

function module_test_tg() {
    local alias="${1:-$SSH_ALIAS}"
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh test-tg <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh test-tg <别名>)"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🧪 Triggering Telegram sample alert preview for [$alias]..."
    else
        echo "🧪 触发 Telegram 通知全套样张预览: [$alias]..."
    fi

    sync_node_scripts "$alias" >/dev/null 2>&1 || true

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    local REMOTE_HOME=$(ssh "${ssh_opts[@]}" "$alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    ssh "${ssh_opts[@]}" "$alias" "IS_TEST_MODE=1 bash ${DOCKER_APP_DIR}/reality_check.sh --test-tg"
    local test_status=$?
    if [ $test_status -ne 0 ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Telegram preview test failed on [$alias] (Exit Code: $test_status)"
        else
            echo "❌ 节点 [$alias] Telegram 样张推送测试失败 (退出码: $test_status)"
        fi
        return $test_status
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Telegram sample pack pushed successfully!"
    else
        echo "✅ Telegram 通知样张推送完成！"
    fi
    return 0
}

function module_test_sni() {
    local alias="${1:-$SSH_ALIAS}"
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh test-sni <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh test-sni <别名>)"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🧪 Evaluating SNI scores (dry-run simulation) for [$alias]..."
    else
        echo "🧪 纯选域演练 (评测 SNI 得分): [$alias]..."
    fi

    sync_node_scripts "$alias" >/dev/null 2>&1 || true

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    local REMOTE_HOME=$(ssh "${ssh_opts[@]}" "$alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    ssh "${ssh_opts[@]}" "$alias" "
        set -e
        if [ ! -f ${DOCKER_APP_DIR}/runner.sh ] && [ ! -f ${DOCKER_APP_DIR}/reality_rotate.sh ]; then
            echo '❌ 未找到轮换脚本！'
            exit 1
        fi
        cd ${DOCKER_APP_DIR}
        if [ -f ./reality_rotate.sh ]; then
            IS_TEST_MODE=1 DRY_RUN=1 bash ./reality_rotate.sh --force
        elif [ -f ./runner.sh ]; then
            IS_TEST_MODE=1 DRY_RUN=1 bash ./runner.sh --force
        fi
    "
    local sni_status=$?
    if [ $sni_status -ne 0 ]; then
        return $sni_status
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ SNI score evaluation (dry-run) complete."
    else
        echo "✅ SNI 选域演练评估完成。"
    fi
    return 0
}

function module_test() {
    local alias="$1"
    local sub="${2:-tg}"

    # Handle argument permutations like test tg <alias> or test <alias> tg
    if [[ "$alias" == "tg" || "$alias" == "sni" ]]; then
        sub="$alias"
        alias="${2:-$SSH_ALIAS}"
    fi

    if [ "$sub" = "sni" ]; then
        module_test_sni "$alias"
    else
        module_test_tg "$alias"
    fi
}
