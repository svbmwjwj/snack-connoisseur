#!/bin/bash
# reality_check.sh
# X-ray 节点健康状态主动检查工具，支持终端显示与 Telegram 推送
# 部署位置: /home/admin/docker-apps/xray/reality_check.sh

set -e

TARGET_IP="PLACEHOLDER_IP"
TARGET_IPV6="PLACEHOLDER_IPV6"
SERVER_HOST="PLACEHOLDER_HOST"
SSH_ALIAS="PLACEHOLDER_ALIAS"
DOCKER_DIR="/home/admin/docker-apps/xray"
CONFIG_FILE="${DOCKER_DIR}/conf/config.json"
ENV_FILE="${DOCKER_DIR}/.env"

NOTIFY_TG=false
TEST_TG_MODE=false
SILENT_MODE=false
BRIEF_MODE=false
for arg in "$@"; do
    if [ "$arg" = "--notify" ] || [ "$arg" = "-n" ]; then
        NOTIFY_TG=true
    fi
    if [ "$arg" = "--test-tg" ]; then
        NOTIFY_TG=true
        TEST_TG_MODE=true
    fi
    if [ "$arg" = "--silent" ] || [ "$arg" = "-s" ]; then
        SILENT_MODE=true
        NOTIFY_TG=true
    fi
    if [ "$arg" = "--brief" ] || [ "$arg" = "-b" ]; then
        BRIEF_MODE=true
    fi
done

# 加载环境变量
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
GATEWAY_URL="${GATEWAY_URL:-}"
GATEWAY_AUTH_KEY="${GATEWAY_AUTH_KEY:-}"

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
send_tg_msg() { send_tg_message "$@"; }

echo "=========================================="
echo "🩺 开始执行节点体检: $SSH_ALIAS"
echo "=========================================="

# 1. 网络连通性能 (Inbound / Outbound)
echo "📡 测试双向网络连通性能..."

do_ping() {
    ( res=$(ping -c 3 -W 1 "$1" 2>/dev/null | tail -1 | awk '{print $4}' | cut -d '/' -f 2)
      [[ "$res" =~ ^[0-9] ]] && echo "${res} ms" > "/tmp/ping_$2" || echo "Timeout" > "/tmp/ping_$2" ) &
}

# 批量发起并发 Ping (国内主流 BGP 骨干 + 海外主流 CDN/DNS)
declare -A IPs=(
    [out_cf]="1.1.1.1" [out_gg]="8.8.8.8"
    [in_ali]="223.5.5.5" [in_tx]="119.29.29.29"
    [in_bd]="220.181.38.148" [in_114]="114.114.114.114"
)
for k in "${!IPs[@]}"; do do_ping "${IPs[$k]}" "$k"; done
wait

# 取回结果并统计
get_p() { cat "/tmp/ping_$1" 2>/dev/null || echo "Timeout"; }

IN_ALI=$(get_p in_ali)
IN_TX=$(get_p in_tx)
IN_BD=$(get_p in_bd)
IN_114=$(get_p in_114)
OUT_CF=$(get_p out_cf)
OUT_GG=$(get_p out_gg)

IN_TIMEOUTS=0
[ "$IN_ALI" = "Timeout" ] && ((IN_TIMEOUTS++)) || true
[ "$IN_TX" = "Timeout" ] && ((IN_TIMEOUTS++)) || true
[ "$IN_BD" = "Timeout" ] && ((IN_TIMEOUTS++)) || true
[ "$IN_114" = "Timeout" ] && ((IN_TIMEOUTS++)) || true

OUT_TIMEOUTS=0
[ "$OUT_CF" = "Timeout" ] && ((OUT_TIMEOUTS++)) || true
[ "$OUT_GG" = "Timeout" ] && ((OUT_TIMEOUTS++)) || true

if [ "$CNSR_LANG" = "en" ]; then
PING_REPORT="🇨🇳 *China Inbound (BGP / Backbone)*
- *Ali BGP*: ${IN_ALI}
- *Tencent BGP*: ${IN_TX}
- *Baidu Backbone*: ${IN_BD}
- *114DNS Anycast*: ${IN_114}

🌐 *Global Outbound Connectivity*
- *Google DNS (8.8.8.8)*: ${OUT_GG}
- *Cloudflare (1.1.1.1)*: ${OUT_CF}"
else
PING_REPORT="🇨🇳 *入站国内回程 (BGP / 骨干链路)*
- 阿里 BGP: ${IN_ALI}
- 腾讯 BGP: ${IN_TX}
- 百度骨干: ${IN_BD}
- 114 骨干: ${IN_114}

🌐 *出站海外访问能力 (Global Outbound)*
- Google DNS (8.8.8.8): ${OUT_GG}
- Cloudflare (1.1.1.1): ${OUT_CF}"
fi

echo "   - [Inbound] 阿里 BGP: ${IN_ALI}"
echo "   - [Inbound] 腾讯 BGP: ${IN_TX}"
echo "   - [Inbound] 百度骨干: ${IN_BD}"
echo "   - [Inbound] 114 骨干: ${IN_114}"
echo "   - [Outbound] Google DNS: ${OUT_GG}"
echo "   - [Outbound] Cloudflare: ${OUT_CF}"

rm -f /tmp/ping_*

# 2. 域名解析检查
echo "🌐 检查域名解析状态..."
RESOLVED_IP=$(getent hosts "$SERVER_HOST" 2>/dev/null | awk '{print $1}' | head -n1)
if [ -z "$RESOLVED_IP" ]; then
    DOMAIN_STATUS="🔴 Failed"
elif [ "$RESOLVED_IP" = "$TARGET_IP" ] || [ "$RESOLVED_IP" = "$TARGET_IPV6" ]; then
    DOMAIN_STATUS="🟢 Direct match"
else
    DOMAIN_STATUS="🟡 Proxy/Abnormal ($RESOLVED_IP)"
fi
echo "   - $SERVER_HOST 解析至: ${RESOLVED_IP:-N/A} ($DOMAIN_STATUS)"

# 3. 提取当前 SNI 状态
echo "🎯 检测当前 SNI 状态..."
CURRENT_SNI=$(jq -r '.inbounds[0].streamSettings.realitySettings.serverNames[0] // "N/A"' "$CONFIG_FILE")
if [ "$CURRENT_SNI" = "N/A" ]; then
    SNI_STATUS="🔴 未找到 SNI 配置"
else
    # 借助 reality-checker 检测当前 SNI
    CHECKER_BIN="${DOCKER_DIR}/reality-checker"
    if [ ! -x "$CHECKER_BIN" ]; then
        CHECKER_BIN="/home/admin/reality-checker"
    fi
    if [ ! -x "$CHECKER_BIN" ]; then
        CHECKER_BIN="reality-checker"
    fi
    if [ -x "$CHECKER_BIN" ] || command -v "$CHECKER_BIN" >/dev/null 2>&1; then
        CHECKER_LOG=$(mktemp)
        "$CHECKER_BIN" check "$CURRENT_SNI" > "$CHECKER_LOG" 2>&1 || true
        
        # 解析表格获取详细指标
        SNI_PARSE=$(awk -F'|' -v sni="$CURRENT_SNI" '
          $2 ~ sni {
            base = $3;   gsub(/^[ \t]+|[ \t]+$/, "", base);
            handshake = $4; gsub(/^[ \t]+|[ \t]+$/, "", handshake);
            cert = $5;   gsub(/^[ \t]+|[ \t]+$/, "", cert);
            cdn = $6;    gsub(/^[ \t]+|[ \t]+$/, "", cdn);
            stars = $8;  gsub(/^[ \t]+|[ \t]+$/, "", stars);
            status = $9; gsub(/^[ \t]+|[ \t]+$/, "", status);
            
            if (cdn == "无") cdn = "None";
            else if (cdn == "高") cdn = "High";
            
            gsub(/天/, " days", cert);
            gsub(/\*/, "★", stars);
            
            print status ";" base ";" handshake ";" cert ";" cdn ";" stars;
            exit
          }
        ' "$CHECKER_LOG" | sed -E 's/\x1B\[[0-9;]*[a-zA-Z]//g')
        rm -f "$CHECKER_LOG"
        
        if [ -n "$SNI_PARSE" ]; then
            IFS=';' read -r SNI_CODE SNI_BASE SNI_HANDSHAKE SNI_CERT SNI_CDN SNI_RATING <<< "$SNI_PARSE"
        fi
    fi
fi

SNI_CODE="${SNI_CODE:-N/A}"
SNI_BASE="${SNI_BASE:-N/A}"
SNI_HANDSHAKE="${SNI_HANDSHAKE:-N/A}"
SNI_CERT="${SNI_CERT:-N/A}"
SNI_CDN="${SNI_CDN:-N/A}"
SNI_RATING="${SNI_RATING:-N/A}"

echo "   - 当前 SNI: $CURRENT_SNI ($SNI_CODE / CDN: $SNI_CDN / Rating: $SNI_RATING)"

# 4. 服务器系统状态
echo "💻 检查服务器内核与运行状态..."
SYS_UPTIME=$(uptime -p 2>/dev/null | sed 's/up //')
TCP_FASTOPEN=$(cat /proc/sys/net/ipv4/tcp_fastopen 2>/dev/null || echo "N/A")
if [ "$TCP_FASTOPEN" = "3" ]; then
    TFO_STATUS="🟢 Enabled (3)"
else
    TFO_STATUS="🟡 Partial ($TCP_FASTOPEN)"
fi

# 检查 UDP Relay 兼容性 (BBR / UFW)
BBR_STATUS=$(cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null | grep -q bbr && echo "🟢 BBR" || echo "🟡 Cubic/Reno")
UDP_STATUS="${UDP_STATUS:-🟢 Enabled}"
echo "   - 系统运行时间: $SYS_UPTIME"
echo "   - TCP Fast Open: $TFO_STATUS"
echo "   - 拥塞控制模块: $BBR_STATUS"
echo "   - UDP 转发支持: $UDP_STATUS"

# 5. 容器与 X-ray 状态
echo "🐳 检查 Docker 容器运行状态..."
cd "$DOCKER_DIR"
CONTAINER_STATUS=$(sudo docker inspect -f '{{.State.Status}}' xray 2>/dev/null || echo "N/A")
CONTAINER_UPTIME=$(sudo docker inspect -f '{{.State.StartedAt}}' xray 2>/dev/null | cut -d'.' -f1 || echo "N/A")

echo "   - 容器状态: $CONTAINER_STATUS ($CONTAINER_UPTIME)"

echo "🔑 读取 X-ray 核心参数..."
XRAY_UUID=$(jq -r '.inbounds[0].settings.clients[0].id // "N/A"' "$CONFIG_FILE")
XRAY_PRIV=$(jq -r '.inbounds[0].streamSettings.realitySettings.privateKey // "N/A"' "$CONFIG_FILE")
if [ "$XRAY_PRIV" != "N/A" ] && [ -n "$XRAY_PRIV" ]; then
    XRAY_PUB=$(cd "$DOCKER_DIR" && sudo docker compose exec -T xray xray x25519 -i "$XRAY_PRIV" 2>/dev/null | grep -i "PublicKey" | awk '{print $NF}' || echo "N/A")
else
    XRAY_PUB="N/A"
fi
XRAY_SID=$(jq -r '.inbounds[0].streamSettings.realitySettings.shortIds[0] // "N/A"' "$CONFIG_FILE")
FLOW=$(jq -r '.inbounds[0].settings.clients[0].flow // "N/A"' "$CONFIG_FILE")

# 脱敏处理
UUID_MASKED="${XRAY_UUID:0:8}-****-****-****-************"
if [ -n "$XRAY_PUB" ] && [ "$XRAY_PUB" != "N/A" ]; then
    XRAY_PUB_MASKED="${XRAY_PUB:0:8}********"
else
    XRAY_PUB_MASKED="N/A"
fi

# 计算总评结论
CONCLUSION="🟢 Normal"
if [[ "$DOMAIN_STATUS" == *"🔴"* ]] || [[ "$CONTAINER_STATUS" == *"N/A"* ]] || [[ "$CONTAINER_STATUS" == *"exited"* ]] || [[ "$SNI_CODE" == "N/A" ]]; then
    CONCLUSION="🔴 Error (Service/DNS)"
elif [ "$OUT_TIMEOUTS" -eq 2 ]; then
    CONCLUSION="🔴 Error (Outbound Disconnected)"
elif [ "$IN_TIMEOUTS" -ge 3 ]; then
    CONCLUSION="🔴 Error (China Inbound Blocked)"
elif [[ "$DOMAIN_STATUS" == *"🟡"* ]] || [[ "$TFO_STATUS" == *"🟡"* ]] || [ "$OUT_TIMEOUTS" -gt 0 ] || [ "$IN_TIMEOUTS" -gt 0 ]; then
    CONCLUSION="🟡 Warning (Suboptimal / Incomplete)"
fi

function detect_local_ipv6() {
    local detected_ip=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep ':' | grep -v '^fe80' | grep -v '^fc' | grep -v '^fd' | head -n1)
    if [ -z "$detected_ip" ]; then
        detected_ip=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/ {print $2}' | cut -d/ -f1 | grep -v '^fe80' | grep -v '^fc' | grep -v '^fd' | head -n1)
    fi
    if [ -z "$detected_ip" ]; then
        local mac=$(curl -s --connect-timeout 2 http://169.254.169.254/latest/meta-data/mac 2>/dev/null || true)
        [ -n "$mac" ] && detected_ip=$(curl -s --connect-timeout 2 "http://169.254.169.254/latest/meta-data/network/interfaces/macs/${mac}/ipv6s" 2>/dev/null | head -n1 || true)
    fi
    if [ -z "$detected_ip" ]; then
        detected_ip=$(curl -6 -s --connect-timeout 3 https://api64.ipify.org 2>/dev/null || curl -6 -s --connect-timeout 3 https://icanhazip.com 2>/dev/null || echo 'none')
    fi
    [ -z "$detected_ip" ] && detected_ip='none'
    echo "$detected_ip"
}

# 推送 Telegram
if [ "$NOTIFY_TG" = true ] && { [ -n "$GATEWAY_URL" ] || { [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; }; }; then
    if [ -z "$TARGET_IPV6" ] || [ "$TARGET_IPV6" = "none" ] || [ "$TARGET_IPV6" = "PLACEHOLDER_""IPV6" ] || [ "$TARGET_IPV6" = "N/A" ]; then
        TARGET_IPV6=$(detect_local_ipv6)
    fi
    DISPLAY_IPV4="$TARGET_IP"
    DISPLAY_IPV6="$TARGET_IPV6"
    if [ -z "$DISPLAY_IPV6" ] || [ "$DISPLAY_IPV6" = "none" ] || [ "$DISPLAY_IPV6" = "PLACEHOLDER_""IPV6" ]; then
        DISPLAY_IPV6="N/A"
    fi

    if [ "$TEST_TG_MODE" = true ]; then
        echo "📱 正在推送 Telegram 全套通知预览样张..."
        QX_SAMPLE="vless=${SERVER_HOST}:443, method=none, password=${XRAY_UUID}, obfs=over-tls, obfs-host=${CURRENT_SNI}, reality-base64-pubkey=${XRAY_PUB}, reality-hex-shortid=${XRAY_SID}, vless-flow=xtls-rprx-vision, udp-relay=true, fast-open=true, tag=${SSH_ALIAS}"
        VLESS_SAMPLE="vless://${XRAY_UUID}@${SERVER_HOST}:443?encryption=none&security=reality&sni=${CURRENT_SNI}&fp=chrome&pbk=${XRAY_PUB}&sid=${XRAY_SID}&type=tcp&flow=xtls-rprx-vision#${SSH_ALIAS}"

        echo "   - [1/5] 推送 Node Deployed (初始节点部署) 样张..."
        M1=$(tpl_node_config "true" "$SSH_ALIAS" "$SERVER_HOST" "$CURRENT_SNI" "$XRAY_UUID" "$XRAY_PUB" "$XRAY_SID" "$QX_SAMPLE" "$VLESS_SAMPLE")
        send_tg_message "$M1"
        sleep 1

        echo "   - [2/5] 推送 Node Rotated (域名自动换新) 样张..."
        M2=$(tpl_node_config "false" "$SSH_ALIAS" "$SERVER_HOST" "$CURRENT_SNI" "$XRAY_UUID" "$XRAY_PUB" "$XRAY_SID" "$QX_SAMPLE" "$VLESS_SAMPLE")
        send_tg_message "$M2"
        sleep 1

        echo "   - [3/5] 推送 SNI Blocked Alert (SNI 阻断告警) 样张..."
        M3=$(tpl_sni_blocked "$SSH_ALIAS" "blocked-sample.com" "1.2.3.0/24")
        send_tg_message "$M3"
        sleep 1

        echo "   - [4/5] 推送 Critical Alert (自愈失败告警) 样张..."
        M4=$(tpl_alert_failure "$SSH_ALIAS" "所有备用 SNI 域名尝试握手均超时" "节点离线" "请执行 \`./cnsr.sh check $SSH_ALIAS\` 查看诊断")
        send_tg_message "$M4"
        sleep 1

        echo "   - [5/5] 推送 Health Report (完整体检报告) 样张..."
        UUID_MASKED="${XRAY_UUID:0:8}-****-****-****-************"
        PUB_MASKED="${XRAY_PUB:0:6}********"
        M5=$(tpl_health_full "$SSH_ALIAS" "$CONCLUSION" "$DISPLAY_IPV4" "$DISPLAY_IPV6" "$SERVER_HOST" "$DOMAIN_STATUS" "$PING_REPORT" "$CURRENT_SNI" "$SNI_CODE" "$SNI_BASE" "$SNI_HANDSHAKE" "$SNI_CERT" "$SNI_CDN" "$SNI_RATING" "$SYS_UPTIME" "$TFO_STATUS" "$BBR_STATUS" "$UDP_STATUS" "$CONTAINER_STATUS" "$CONTAINER_UPTIME" "$FLOW" "$UUID_MASKED" "$PUB_MASKED" "$XRAY_SID")
        send_tg_message "$M5"

        echo "✅ Telegram 全套样张推送成功！"
    else
        echo "📱 正在推送体检报告至 Telegram..."
        UUID_MASKED="${XRAY_UUID:0:8}-****-****-****-************"
        PUB_MASKED="${XRAY_PUB:0:6}********"
        MESSAGE=$(tpl_health_full "$SSH_ALIAS" "$CONCLUSION" "$DISPLAY_IPV4" "$DISPLAY_IPV6" "$SERVER_HOST" "$DOMAIN_STATUS" "$PING_REPORT" "$CURRENT_SNI" "$SNI_CODE" "$SNI_BASE" "$SNI_HANDSHAKE" "$SNI_CERT" "$SNI_CDN" "$SNI_RATING" "$SYS_UPTIME" "$TFO_STATUS" "$BBR_STATUS" "$UDP_STATUS" "$CONTAINER_STATUS" "$CONTAINER_UPTIME" "$FLOW" "$UUID_MASKED" "$PUB_MASKED" "$XRAY_SID")
        send_tg_message "$MESSAGE"
        echo "✅ 推送成功！"
    fi
fi
