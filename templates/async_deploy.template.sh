#!/bin/bash
# async_deploy.sh - Polling GitHub and executing deploy
# 部署位置: /home/admin/docker-apps/xray/async_deploy.sh

set -e

TARGET_IP="PLACEHOLDER_IP"
SSH_ALIAS="PLACEHOLDER_ALIAS"
DOCKER_DIR="/home/admin/docker-apps/xray"
ENV_FILE="${DOCKER_DIR}/.env"

# 加载环境变量
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
GATEWAY_URL="${GATEWAY_URL:-}"
GATEWAY_AUTH_KEY="${GATEWAY_AUTH_KEY:-}"

if [ -f "${DOCKER_DIR}/tg_templates.sh" ]; then
    source "${DOCKER_DIR}/tg_templates.sh"
fi

function send_tg_message() {
    local text="$1"
    local resp=""
    local http_code=0

    if [ -n "$GATEWAY_URL" ]; then
        local payload
        payload=$(jq -n --arg text "$text" '{text: $text, parse_mode: "Markdown"}')
        local auth_header=()
        if [ -n "$GATEWAY_AUTH_KEY" ]; then
            auth_header=(-H "Authorization: Bearer $GATEWAY_AUTH_KEY")
        fi
        resp=$(curl -s -w "\n%{http_code}" -X POST "${auth_header[@]}" \
            -H "Content-Type: application/json" \
            "$GATEWAY_URL/api/tg" \
            -d "$payload" 2>/dev/null || echo -e "\n000")
        http_code=$(echo "$resp" | tail -n1)
        local body=$(echo "$resp" | sed '$d')
        if [[ "$http_code" =~ ^2[0-9]{2}$ ]] || [ -z "$http_code" ]; then
            return 0
        else
            echo "⚠️ [TG] Gateway 推送失败 (HTTP $http_code): $body" >&2
            if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
                echo "🔄 [TG] 尝试降级为直连 Telegram 推送..." >&2
            else
                return 1
            fi
        fi
    fi

    if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
        resp=$(curl -s -w "\n%{http_code}" -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TG_CHAT_ID}" \
            -d parse_mode="Markdown" \
            --data-urlencode "text=${text}" 2>/dev/null || echo -e "\n000")
        http_code=$(echo "$resp" | tail -n1)
        local body=$(echo "$resp" | sed '$d')
        if [[ "$http_code" =~ ^2[0-9]{2}$ ]] || [ -z "$http_code" ]; then
            return 0
        else
            echo "⚠️ [TG] 直连推送失败 (HTTP $http_code): $body" >&2
            return 1
        fi
    fi
}

echo "🚀 异步部署守护进程启动... 等待 GitHub Actions 云端扫描完成 ($TARGET_IP)"

# 轮询获取 GitHub Release
POLL_INTERVAL=10
MAX_ATTEMPTS=60 # 等待最多 10 分钟
ATTEMPT=0
FOUND=false

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if [ -n "$GATEWAY_URL" ]; then
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $GATEWAY_AUTH_KEY" \
            "$GATEWAY_URL/api/release/${SSH_ALIAS}.csv" 2>/dev/null || true)
        if [ "$HTTP_CODE" = "200" ]; then
            FOUND=true
            break
        fi
    elif [ -n "$GH_TOKEN" ]; then
        ASSET_URL=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
            https://api.github.com/repos/svbmwjwj/snack-connoisseur/releases/tags/latest 2>/dev/null \
            | jq -r ".assets[]? | select(.name==\"${SSH_ALIAS}.csv\") | .url" 2>/dev/null || true)
        
        if [ -n "$ASSET_URL" ] && [ "$ASSET_URL" != "null" ]; then
            FOUND=true
            break
        fi
    else
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -L "https://github.com/svbmwjwj/snack-connoisseur/releases/download/latest/${SSH_ALIAS}.csv")
        if [ "$HTTP_CODE" = "200" ]; then
            FOUND=true
            break
        fi
    fi
    sleep $POLL_INTERVAL
    ATTEMPT=$((ATTEMPT+1))
done

DEBUG_MODE=false
if [ "$1" = "--debug" ]; then
    DEBUG_MODE=true
fi

if [ "$FOUND" = true ]; then
    cd "$DOCKER_DIR"
    # 执行强制轮换与部署，完成后将自动触发 tpl_node_config 通知
    if [ "$DEBUG_MODE" = true ]; then
        ./reality_rotate.sh --force --init
    else
        ./reality_rotate.sh --force --init > rotate_async.log 2>&1
    fi
    
    echo "✅ 异步部署全流程完成！"
else
    # 超时失败，发送致命告警
    MSG=$(tpl_alert_failure "$SSH_ALIAS" "云端扫描 CSV 超时 (10分钟未生成)" "初始部署受阻" "请检查 GitHub Actions 状态或在节点上执行 ./cnsr.sh init 重试")
    send_tg_message "$MSG"
fi

# 任务完成，自行销毁
rm -f "$0"
