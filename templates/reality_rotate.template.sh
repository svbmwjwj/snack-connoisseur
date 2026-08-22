#!/bin/bash
# reality_rotate.sh
# 运行于 Debian / Ubuntu 服务器。每 15 分钟由 crontab 调用。
# 包含 3 级递进防误判机制 (30s -> 60s)。若确认损坏，彻底清理旧 CSV，从云端拉取最新扫描库并精选替换。

set -e

[ -n "$IS_TEST_MODE" ] && export IS_TEST_MODE

TARGET_IP="PLACEHOLDER_IP"
SERVER_HOST="PLACEHOLDER_HOST"
SSH_ALIAS="PLACEHOLDER_ALIAS"
TARGET_CIDR_V4="PLACEHOLDER_CIDR_V4"
TARGET_CIDR_V6="PLACEHOLDER_CIDR_V6"
DOCKER_DIR="/home/admin/docker-apps/xray"
CSV_FILE="${DOCKER_DIR}/${SSH_ALIAS}.csv"
CONFIG_FILE="${DOCKER_DIR}/conf/config.json"
NEW_DOMAIN_TXT="${DOCKER_DIR}/new_domain.txt"
ENV_FILE="${DOCKER_DIR}/.env"

# 加载环境变量 (Telegram / Gateway 配置)
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

if [ -f "${DOCKER_DIR}/tg_templates.sh" ]; then
    source "${DOCKER_DIR}/tg_templates.sh"
fi

TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
GH_TOKEN="${GH_TOKEN:-}"
GATEWAY_URL="${GATEWAY_URL:-}"
GATEWAY_AUTH_KEY="${GATEWAY_AUTH_KEY:-}"

# GitHub Release CSV 下载地址
GITHUB_CSV_URL="https://github.com/svbmwjwj/snack-connoisseur/releases/download/latest/${SSH_ALIAS}.csv"

# 检查依赖
if ! command -v jq >/dev/null 2>&1; then
    echo "❌ 错误: 系统未安装 jq。"
    exit 1
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

function send_gh_dispatch() {
    local target_ip="$1"
    local cidr_v4="$2"
    local cidr_v6="$3"
    local req_ts="$4"

    if [ -n "$GATEWAY_URL" ]; then
        local payload
        payload=$(jq -n \
            --arg ip "$target_ip" \
            --arg v4 "$cidr_v4" \
            --arg v6 "$cidr_v6" \
            --arg ts "$req_ts" \
            --arg alias "$SSH_ALIAS" \
            '{target_ip: $ip, target_cidr_v4: $v4, target_cidr_v6: $v6, timestamp: $ts, alias: $alias}')
        local auth_header=()
        if [ -n "$GATEWAY_AUTH_KEY" ]; then
            auth_header=(-H "Authorization: Bearer $GATEWAY_AUTH_KEY")
        fi
        curl -s -X POST "${auth_header[@]}" \
            -H "Content-Type: application/json" \
            "$GATEWAY_URL/api/gh-dispatch" \
            -d "$payload" >/dev/null 2>&1 || true
    elif [ -n "$GH_TOKEN" ]; then
        local payload
        payload=$(jq -n \
            --arg ip "$target_ip" \
            --arg v4 "$cidr_v4" \
            --arg v6 "$cidr_v6" \
            --arg ts "$req_ts" \
            --arg alias "$SSH_ALIAS" \
            '{event_type: "scan_trigger", client_payload: {target_ip: $ip, target_cidr_v4: $v4, target_cidr_v6: $v6, timestamp: $ts, alias: $alias}}')
        curl -s -X POST \
            -H "Accept: application/vnd.github.v3+json" \
            -H "Authorization: Bearer $GH_TOKEN" \
            https://api.github.com/repos/svbmwjwj/snack-connoisseur/dispatches \
            -d "$payload" >/dev/null 2>&1 || true
    fi
}

FORCE_ROTATE=false
IS_INIT=false
for arg in "$@"; do
    if [ "$arg" = "--force" ] || [ "$arg" = "-f" ]; then
        FORCE_ROTATE=true
    elif [ "$arg" = "--init" ]; then
        IS_INIT=true
    fi
done

CURRENT_SNI=$(jq -r '.inbounds[0].streamSettings.realitySettings.serverNames[0] // ""' "$CONFIG_FILE")
NEED_ROTATE=false

function check_sni_health() {
    if [ "$MOCK_SNI_PROBE_FAIL" = "1" ]; then
        echo "    [MOCK] 强行挂钩，让 check_sni_health 返回失败"
        return 1
    fi
    local domain="$1"
    local checker="${DOCKER_DIR}/reality-checker"
    if [ ! -x "$checker" ]; then
        checker="/home/admin/reality-checker"
    fi
    if [ ! -x "$checker" ]; then
        checker="reality-checker"
    fi
    local check_log=$(mktemp)
    "$checker" check "$domain" > "$check_log" 2>&1 || true
    if grep -q "✓" "$check_log"; then
        rm -f "$check_log"
        return 0
    else
        rm -f "$check_log"
        return 1
    fi
}

echo "🔍 [1/5] 正在执行 3 级防误判健康检查 (当前 SNI: ${CURRENT_SNI:-无})..."

if [ "$FORCE_ROTATE" = true ]; then
    echo "⚠️ 收到强制更换指令 (--force)，直接触发轮换！"
    NEED_ROTATE=true
elif [ -z "$CURRENT_SNI" ]; then
    echo "⚠️ 配置文件中无可用 SNI，触发轮换！"
    NEED_ROTATE=true
else
    NEED_ROTATE=true
    delays=(0 30 60)
    for i in {1..3}; do
        delay=${delays[$((i-1))]}
        if [ "$delay" -gt 0 ]; then
            echo "  ⚠️ 试探 $((i-1))/3 失败！等待 ${delay} 秒后发起第 ${i} 次探测..."
            sleep "$delay"
        fi
        echo "  -> 试探 ${i}/3: 探测 $CURRENT_SNI..."
        if check_sni_health "$CURRENT_SNI"; then
            echo "  ✅ 试探 ${i}/3 恢复正常（网络瞬时抖动），取消轮换。"
            NEED_ROTATE=false
            break
        fi
    done
    if [ "$NEED_ROTATE" = true ]; then
        echo "  ❌ 连续 3 次探测均确定失效！判定域名 [$CURRENT_SNI] 彻底死亡！"
    fi
fi

if [ "$NEED_ROTATE" = false ]; then
    echo "无需更换。系统安全退出。"
    exit 0
fi

echo "🚨 [2/5] 确认域名失效！废弃旧 CSV，呼叫 GitHub Actions 开展全网实时扫描..."

function download_github_csv() {
    local node_alias="$1"
    local req_ts="$2"
    local output_file="$3"
    local patterns=()
    if [ -n "$req_ts" ]; then
        patterns=("${node_alias}_${req_ts}.csv" "${node_alias}.csv")
    else
        patterns=("${node_alias}.csv")
    fi
    
    for target_pattern in "${patterns[@]}"; do
        # 1. 优先通过 Cloudflare Gateway 零凭据安全代理下载（支持 KV 缓存与私有仓库）
        if [ -n "$GATEWAY_URL" ]; then
            local auth_header=()
            if [ -n "$GATEWAY_AUTH_KEY" ]; then
                auth_header=(-H "Authorization: Bearer $GATEWAY_AUTH_KEY")
            fi
            curl -sL -f "${auth_header[@]}" "$GATEWAY_URL/api/storage/${target_pattern}" -o "$output_file" 2>/dev/null || \
            curl -sL -f "${auth_header[@]}" "$GATEWAY_URL/api/release/${target_pattern}" -o "$output_file" 2>/dev/null || true
            if [ -s "$output_file" ]; then
                return 0
            fi
        fi

        # 2. 直连 GitHub API 带 Token 下载
        if [ -n "$GH_TOKEN" ]; then
            local asset_url
            asset_url=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
              https://api.github.com/repos/svbmwjwj/snack-connoisseur/releases/tags/latest 2>/dev/null \
              | jq -r ".assets[]? | select(.name==\"${target_pattern}\") | .url" 2>/dev/null || true)
            
            if [ -n "$asset_url" ] && [ "$asset_url" != "null" ]; then
                curl -sL -H "Authorization: Bearer $GH_TOKEN" \
                     -H "Accept: application/octet-stream" \
                     "$asset_url" -o "$output_file" 2>/dev/null || true
                if [ -s "$output_file" ]; then
                    return 0
                fi
            fi
        fi

        # 3. 尝试公开 Release 路径直接下载
        local direct_url="https://github.com/svbmwjwj/snack-connoisseur/releases/download/latest/${target_pattern}"
        curl -sL -f -o "$output_file" "$direct_url" 2>/dev/null || true
        if [ -s "$output_file" ]; then
            return 0
        fi
    done
    
    return 1
}

# 彻底删除旧 CSV，绝不使用历史过期数据
rm -f "$CSV_FILE"

# 若配置了 GATEWAY_URL 或 GH_TOKEN，则向 GitHub 提交扫描请求并等待新数据打包
if [ -n "$GATEWAY_URL" ] || [ -n "$GH_TOKEN" ]; then
    REQ_TS=$(date +%Y%m%d_%H%M%S)
    echo "☁️ 正在向 GitHub 发送扫描指令 (Target: $TARGET_IP, CIDR: $TARGET_CIDR_V4 / $TARGET_CIDR_V6, TS: $REQ_TS)..."
    
    # 只有在非初次部署且包含有效旧域名时，才发送 SNI 被阻断告警
    if [ "$IS_INIT" != "true" ] && [ "$CURRENT_SNI" != "PLACEHOLDER_SNI" ] && [ -n "$CURRENT_SNI" ]; then
        MSG=$(tpl_sni_blocked "$SSH_ALIAS" "$CURRENT_SNI" "$TARGET_CIDR_V4")
        send_tg_message "$MSG"
    fi

    send_gh_dispatch "$TARGET_IP" "$TARGET_CIDR_V4" "$TARGET_CIDR_V6" "$REQ_TS"

    echo "⏳ 正在轮询等待 GitHub Actions 云端扫描完成并发布产物 (最多等待 240 秒)..."
    for i in {1..24}; do
        if download_github_csv "$SSH_ALIAS" "$REQ_TS" "$CSV_FILE"; then
            echo "✅ 成功从 GitHub Release 拉取最新热扫描库 $CSV_FILE！"
            break
        fi
        sleep 10
    done
else
    echo "⚠️ 未检测到 GATEWAY_URL 或 GH_TOKEN，将跳过云端实时重扫，尝试拉取现有 Release 数据..."
    download_github_csv "$SSH_ALIAS" "" "$CSV_FILE" || true
fi

# 精选高可用 REALITY 备用白名单域名池 (仅在云端扫描超时或无可用域名时作为应急兜底)
FALLBACK_SNI_POOL=(
    "gateway.icloud.com"
    "swdist.apple.com"
    "updates.cdn-apple.com"
    "itunes.apple.com"
    "www.microsoft.com"
    "addons.mozilla.org"
)

NEW_SNI=""

if [ -s "$CSV_FILE" ]; then
    echo "🔍 [3/5] 使用 RealityChecker 深度检测新候选域名..."
    CANDIDATE_DOMAINS=$(awk -F',' 'NR>1 {print $9}' "$CSV_FILE" | sed 's/"//g' | sed 's/\*\.//g' | grep -v 'Fake' | grep '\.' | grep -v "${CURRENT_SNI}" | sort -u | tr '\n' ' ')

    if [ -n "$CANDIDATE_DOMAINS" ]; then
        CHECKER_LOG=$(mktemp)
        CHECKER_BIN="${DOCKER_DIR}/reality-checker"
        if [ ! -x "$CHECKER_BIN" ]; then
            CHECKER_BIN="/home/admin/reality-checker"
        fi
        if [ ! -x "$CHECKER_BIN" ]; then
            CHECKER_BIN="reality-checker"
        fi
        if [ -x "$CHECKER_BIN" ] || command -v "$CHECKER_BIN" >/dev/null 2>&1; then
            "$CHECKER_BIN" batch $CANDIDATE_DOMAINS > "$CHECKER_LOG" 2>&1 || true
        else
            echo "⚠️ reality-checker 未安装，跳过批量检测..." > "$CHECKER_LOG"
        fi

        cat "$CHECKER_LOG"

        # 3.3 严密解析表格，严格考量全部 7 项指标 (基础条件✓、无CDN、页面状态200、四星/五星推荐)
        NEW_SNI=$(sed -E 's/\x1B\[[0-9;]*[a-zA-Z]//g' "$CHECKER_LOG" | awk -F'|' '
          /\|/ && $2 !~ /最终域名/ && $2 !~ /^\s*\+-/ {
            domain = $2; gsub(/^[ \t]+|[ \t]+$/, "", domain);
            base = $3;   gsub(/^[ \t]+|[ \t]+$/, "", base);
            cdn = $6;    gsub(/^[ \t]+|[ \t]+$/, "", cdn);
            stars = $8;  gsub(/^[ \t]+|[ \t]+$/, "", stars);
            status = $9; gsub(/^[ \t]+|[ \t]+$/, "", status);
            
            if (base ~ /✓/ && cdn == "无" && (stars ~ /\*\*\*\*\*/ || stars ~ /\*\*\*\*/) && status == "200") {
              print domain;
            }
          }
        ' | grep -v "${CURRENT_SNI}" | head -n 1)
        rm -f "$CHECKER_LOG"
    fi
fi

# 如果未从云端扫描结果中获取到 5 星域名（扫描超时/CSV为空/无符合条件域名），启用应急高可用备用池
if [ -z "$NEW_SNI" ]; then
    echo "⚠️ 未能从云端扫描结果中获取到高星域名，尝试从应急高可用备用池挑选可用 SNI..."
    for fb in "${FALLBACK_SNI_POOL[@]}"; do
        if [ "$fb" != "$CURRENT_SNI" ]; then
            echo "  -> 测试备用池域名: $fb..."
            if check_sni_health "$fb"; then
                NEW_SNI="$fb"
                echo "🛡️ 成功通过探测选定应急备用 SNI: $NEW_SNI"
                break
            fi
        fi
    done
fi

# 如果经过探测仍未匹配到（可能本地 checker 异常），直接使用第一备用域兜底保证节点存活
if [ -z "$NEW_SNI" ]; then
    for fb in "${FALLBACK_SNI_POOL[@]}"; do
        if [ "$fb" != "$CURRENT_SNI" ]; then
            NEW_SNI="$fb"
            break
        fi
    done
fi

if [ -z "$NEW_SNI" ]; then
    echo "❌ 严重错误: 无法获取任何可用 SNI 域名。"
    MSG=$(tpl_alert_failure "$SSH_ALIAS" "云端扫描与应急备用池均无法获取有效 SNI" "自愈中断" "请登录 VPS 检查网络或执行 \`./cnsr.sh check $SSH_ALIAS\`")
    send_tg_message "$MSG"
    exit 1
fi

NEW_SNI=$(echo "$NEW_SNI" | tr -d '\r\n"' | sed 's/www\.//g')
echo "✅ 锁定新回落域名 (SNI): $NEW_SNI"

echo "🔑 [4/5] 重新生成 X25519 密钥对、UUID 和 ShortID..."
cd "$DOCKER_DIR"

X25519_OUTPUT=$(sudo docker compose run --rm -T xray x25519 2>/dev/null || sudo docker compose exec -T xray xray x25519)
NEW_PRIVATE_KEY=$(echo "$X25519_OUTPUT" | grep -i "PrivateKey:" | awk '{print $2}' | tr -d '\r\n')
NEW_PUBLIC_KEY=$(echo "$X25519_OUTPUT" | grep -i "Password (PublicKey):" | awk '{print $3}' | tr -d '\r\n')
NEW_UUID=$(sudo docker compose run --rm -T xray uuid 2>/dev/null | tr -d '\r\n' || sudo docker compose exec -T xray xray uuid | tr -d '\r\n')
NEW_SHORT_ID=$(openssl rand -hex 8 | tr -d '\r\n')

echo "----------------------------------------"
echo "新生成的凭据信息："
echo "UUID:        $NEW_UUID"
echo "PublicKey:   $NEW_PUBLIC_KEY"
echo "PrivateKey:  $NEW_PRIVATE_KEY"
echo "ShortID:     $NEW_SHORT_ID"
echo "SNI:         $NEW_SNI"
echo "----------------------------------------"

if [ "$DRY_RUN" = "1" ]; then
    echo "🧪 [DRY-RUN] 拟选定最佳 SNI: $NEW_SNI"
    echo "🧪 [DRY-RUN] 空转演练完成，跳过 config.json 写入与 Docker 重启。"
    exit 0
fi

if jq --arg uuid "$NEW_UUID" \
   --arg privateKey "$NEW_PRIVATE_KEY" \
   --arg shortId "$NEW_SHORT_ID" \
   --arg sni "$NEW_SNI" \
   --arg dest "$NEW_SNI:443" \
   '
   .inbounds[0].settings.clients[0].id = $uuid |
   .inbounds[0].streamSettings.realitySettings.dest = $dest |
   .inbounds[0].streamSettings.realitySettings.serverNames = [$sni] |
   .inbounds[0].streamSettings.realitySettings.privateKey = $privateKey |
   .inbounds[0].streamSettings.realitySettings.shortIds = [$shortId]
   ' "$CONFIG_FILE" > "${CONFIG_FILE}.tmp" && [ -s "${CONFIG_FILE}.tmp" ]; then
    START_TIME=$(date +%s)
    mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
else
    echo "❌ 严重错误：生成新的配置文件失败或文件为空，取消轮换以保护现场。"
    rm -f "${CONFIG_FILE}.tmp"
    MSG=$(tpl_alert_failure "$SSH_ALIAS" "生成新的 X-ray config.json 失败" "配置回滚" "请检查服务器 jq 工具或磁盘空间")
    send_tg_message "$MSG"
    exit 1
fi
sudo docker compose restart xray

echo "⏳ 等待 X-ray 容器启动并检查健康状态..."
sleep 3
if ! sudo docker compose ps --format json xray 2>/dev/null | grep -q "running"; then
    echo "❌ 警告：X-ray 容器未成功启动！"
    sudo docker compose logs --tail 20 xray
    echo "❌ 将终止向 Telegram 推送错误配置，请检查配置文件！"
    MSG=$(tpl_alert_failure "$SSH_ALIAS" "X-ray 容器重启未能正常启动" "节点离线" "请登录 VPS 检查 docker compose logs xray")
    send_tg_message "$MSG"
    exit 1
fi
echo "✅ X-ray 容器启动正常。"

echo "$NEW_SNI" > "$NEW_DOMAIN_TXT"

echo "📱 [5/5] 生成 Quantumult X 与 V2rayN 配置..."

if [ -n "$GATEWAY_URL" ] || { [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; }; then
    PORT="443"
    FLOW=$(jq -r '.inbounds[0].settings.clients[0].flow // "xtls-rprx-vision"' "$CONFIG_FILE")
    QX_CONFIG="vless=${SERVER_HOST}:${PORT}, method=none, password=${NEW_UUID}, obfs=over-tls, obfs-host=${NEW_SNI}, reality-base64-pubkey=${NEW_PUBLIC_KEY}, reality-hex-shortid=${NEW_SHORT_ID}, vless-flow=${FLOW}, udp-relay=true, fast-open=true, tag=${SSH_ALIAS}"
    VLESS_URI="vless://${NEW_UUID}@${SERVER_HOST}:${PORT}?encryption=none&security=reality&sni=${NEW_SNI}&fp=chrome&pbk=${NEW_PUBLIC_KEY}&sid=${NEW_SHORT_ID}&type=tcp&flow=${FLOW}#${SSH_ALIAS}"

    if [ "$BATCH_MODE" = "true" ] && [ "$IS_INIT" = "true" ]; then
        echo "$VLESS_URI" > "${DOCKER_DIR}/vless.txt"
        echo "$QX_CONFIG" > "${DOCKER_DIR}/qx.txt"
        echo "✅ 批量编排初始化模式：配置已写入本地 ${DOCKER_DIR}/vless.txt 和 qx.txt，静默单节点推送以防止刷屏。"
    else
        MESSAGE=$(tpl_node_config "$IS_INIT" "$SSH_ALIAS" "$SERVER_HOST" "$NEW_SNI" "$NEW_UUID" "$NEW_PUBLIC_KEY" "$NEW_SHORT_ID" "$QX_CONFIG" "$VLESS_URI")
        send_tg_message "$MESSAGE"
        echo "✅ 配置推送到 Telegram 成功！"
    fi
fi

echo "🩺 启动轮换后自动体检..."
if [ -f "${DOCKER_DIR}/reality_check.sh" ]; then
    if [ "$BATCH_MODE" = "true" ] && [ "$IS_INIT" = "true" ]; then
        bash "${DOCKER_DIR}/reality_check.sh" --notify-on-error || true
    else
        bash "${DOCKER_DIR}/reality_check.sh" --notify || true
    fi
fi

echo "🎉 全流程同步轮换与体检完成！"
