#!/bin/bash
# Snack Connoisseur - Print Configurations & IPs Module
# Fetches vless, qx configurations or public IPs from remote nodes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || pwd)"
LIB_DIR="$REPO_DIR/lib"

if [ -f "$LIB_DIR/ui.sh" ]; then source "$LIB_DIR/ui.sh"; fi
if [ -f "$LIB_DIR/ssh.sh" ]; then source "$LIB_DIR/ssh.sh"; fi

if [ -f "$REPO_DIR/.env" ]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
fi

function push_tg_msg() {
    local text="$1"

    uv run python3 -c "
import os, sys, json, urllib.request, urllib.parse

text = sys.argv[1]
gateway_url = os.environ.get('GATEWAY_URL', '')
gateway_auth = os.environ.get('GATEWAY_AUTH_KEY', '')
bot_token = os.environ.get('TG_BOT_TOKEN', '')
chat_id = os.environ.get('TG_CHAT_ID', '')

sent = False
if gateway_url:
    headers = {'Content-Type': 'application/json', 'User-Agent': 'curl/8.7.1'}
    if gateway_auth:
        headers['Authorization'] = f'Bearer {gateway_auth}'
    req = urllib.request.Request(
        f'{gateway_url}/api/tg',
        data=json.dumps({'text': text, 'parse_mode': 'Markdown'}).encode('utf-8'),
        headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201, 204):
                sent = True
    except Exception:
        pass

if not sent and bot_token and chat_id:
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'parse_mode': 'Markdown',
        'text': text
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{bot_token}/sendMessage',
        data=data,
        headers={'User-Agent': 'curl/8.7.1'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            sent = True
    except Exception:
        pass
" "$text"
}

function push_links_to_tg() {
    local vless_file="$1"
    local qx_file="$2"
    local count="$3"

    uv run python3 -c "
import os, sys, json, urllib.request, urllib.parse

vless_file = sys.argv[1]
qx_file = sys.argv[2]
count = sys.argv[3]

gateway_url = os.environ.get('GATEWAY_URL', '')
gateway_auth = os.environ.get('GATEWAY_AUTH_KEY', '')
bot_token = os.environ.get('TG_BOT_TOKEN', '')
chat_id = os.environ.get('TG_CHAT_ID', '')

def send_tg(text):
    sent = False
    if gateway_url:
        headers = {'Content-Type': 'application/json', 'User-Agent': 'curl/8.7.1'}
        if gateway_auth:
            headers['Authorization'] = f'Bearer {gateway_auth}'
        req = urllib.request.Request(
            f'{gateway_url}/api/tg',
            data=json.dumps({'text': text, 'parse_mode': 'Markdown'}).encode('utf-8'),
            headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201, 204):
                    sent = True
        except Exception:
            pass

    if not sent and bot_token and chat_id:
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'parse_mode': 'Markdown',
            'text': text
        }).encode('utf-8')
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{bot_token}/sendMessage',
            data=data,
            headers={'User-Agent': 'curl/8.7.1'}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                sent = True
        except Exception:
            pass
    return sent

def push_block(title, filepath, app_hint):
    if not os.path.exists(filepath):
        return
        
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]
        
    if not lines:
        return
    
    chunk_size = 15
    total_chunks = (len(lines) + chunk_size - 1) // chunk_size
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i+chunk_size]
        chunk_idx = i // chunk_size + 1
        part_suffix = f' (分片 {chunk_idx}/{total_chunks})' if total_chunks > 1 else ''
        content = '\n'.join(chunk)
        
        msg = f'{title}{part_suffix} (共 {len(lines)} 条)\n\`\`\`text\n{content}\n\`\`\`\n💡 *适用于*: {app_hint}'
        send_tg(msg)

# 1. 发送 VLESS 链接
push_block('🔗 *VLESS 订阅链接聚合*', vless_file, 'V2rayN / v2rayA / Shadowrocket')

# 2. 发送 QX 链接
push_block('🔗 *Quantumult X 节点配置*', qx_file, 'Quantumult X [server_local]')
" "$vless_file" "$qx_file" "$count"
}

function module_print() {
    local PUSH_TG=false
    local SHOW_IP=false
    local raw_args=()

    for arg in "$@"; do
        if [ "$arg" = "--tg" ]; then
            PUSH_TG=true
        elif [ "$arg" = "--ip" ]; then
            SHOW_IP=true
        else
            raw_args+=("$arg")
        fi
    done

    if [ ${#raw_args[@]} -eq 0 ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias or pattern (e.g. jp_aws-lightsail-1 or jp_aws-lightsail-*)"
        else
            echo "❌ 错误: 请指定节点别名或匹配模式 (例如: jp_aws-lightsail-1 或 jp_aws-lightsail-*)"
        fi
        return 1
    fi

    local target_aliases=()
    for arg in "${raw_args[@]}"; do
        if [[ "$arg" == *"*"* ]]; then
            local base_pattern="${arg%\*}"
            while IFS= read -r line; do
                if [[ "$line" == Host\ $base_pattern* ]]; then
                    local found_alias=$(echo "$line" | awk '{print $2}')
                    target_aliases+=("$found_alias")
                fi
            done < <(grep -i "^Host " ~/.ssh/config 2>/dev/null || true)
        else
            target_aliases+=("$arg")
        fi
    done

    # 去重并自然排序
    if [ ${#target_aliases[@]} -gt 0 ]; then
        IFS=$'\n' target_aliases=($(sort -V -u <<<"${target_aliases[*]}"))
        unset IFS
    fi

    if [ ${#target_aliases[@]} -eq 0 ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ No valid target nodes found in SSH config."
        else
            echo "❌ 未找到任何有效的目标节点。"
        fi
        return 1
    fi

    local count=${#target_aliases[@]}
    local BATCH_TMP_DIR=$(mktemp -d)

    # ----------------------------------------------------
    # 模式 A: --ip 纯 IP 打印 (包含 IPv4 与 IPv6)
    # ----------------------------------------------------
    if [ "$SHOW_IP" = true ]; then
        for cur_alias in "${target_aliases[@]}"; do
            (
                # 本地从 ssh config 获取默认 HostName 作为备用
                local local_v4=""
                if [ -f "$HOME/.ssh/config" ]; then
                    local_v4=$(awk "/^Host $cur_alias\$/{flag=1;next}/^Host /{flag=0}flag && /HostName/{print \$2;exit}" "$HOME/.ssh/config" 2>/dev/null || true)
                fi

                # 远端并发探测真实公网 IPv4 与 IPv6
                local ip_out
                ip_out=$(ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no "$cur_alias" '
                    v4=$(hostname -I 2>/dev/null | tr " " "\n" | grep "\." | grep -v "^127\." | grep -v "^172\." | grep -v "^10\." | head -n1)
                    [ -z "$v4" ] && v4=$(curl -4 -s -m 2 ifconfig.me 2>/dev/null || true)
                    v6=$(hostname -I 2>/dev/null | tr " " "\n" | grep ":" | grep -v "^fe80" | grep -v "^fc" | grep -v "^fd" | head -n1)
                    [ -z "$v6" ] && v6=$(curl -6 -s -m 2 ifconfig.me 2>/dev/null || true)
                    echo "${v4:-none}|${v6:-none}"
                ' 2>/dev/null || echo "${local_v4:-none}|none")

                local final_v4=$(echo "$ip_out" | awk -F'|' '{print $1}')
                local final_v6=$(echo "$ip_out" | awk -F'|' '{print $2}')
                [ -z "$final_v4" ] || [ "$final_v4" = "none" ] && final_v4="${local_v4:-N/A}"
                [ -z "$final_v6" ] || [ "$final_v6" = "none" ] && final_v6="None"

                echo -e "• ${cur_alias}:\n  - IPv4: ${final_v4}\n  - IPv6: ${final_v6}\n" > "${BATCH_TMP_DIR}/${cur_alias}.ip"
            ) &
        done

        wait 2>/dev/null || true

        echo ""
        echo "================================================================================"
        if [ "$CNSR_LANG" = "en" ]; then
            echo "🌐 Node Public IP Directory (Total $count nodes):"
        else
            echo "🌐 节点公网 IP 地址清单 (共 $count 台节点):"
        fi
        echo "--------------------------------------------------------------------------------"
        local tg_ip_text=""
        for cur_alias in "${target_aliases[@]}"; do
            if [ -f "${BATCH_TMP_DIR}/${cur_alias}.ip" ]; then
                cat "${BATCH_TMP_DIR}/${cur_alias}.ip"
                tg_ip_text+="$(cat "${BATCH_TMP_DIR}/${cur_alias}.ip")"
            fi
        done
        echo "================================================================================"
        echo ""

        if [ "$PUSH_TG" = true ]; then
            local tg_title="🌐 *节点公网 IP 地址清单* (共 $count 台)\n\`\`\`text\n${tg_ip_text}\`\`\`"
            push_tg_msg "$tg_title"
            if [ "$CNSR_LANG" = "en" ]; then
                echo "💡 IP address directory successfully pushed to Telegram."
            else
                echo "💡 节点公网 IP 清单已成功推送至 Telegram。"
            fi
        fi

        rm -rf "$BATCH_TMP_DIR"
        return 0
    fi

    # ----------------------------------------------------
    # 模式 B: 默认订阅配置打印 (VLESS / QX)
    # ----------------------------------------------------
    for cur_alias in "${target_aliases[@]}"; do
        (
            local remote_home
            remote_home=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
            local docker_dir="${remote_home}/docker-apps/xray"
            
            local vless_content
            vless_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "cat ${docker_dir}/vless.txt 2>/dev/null" 2>/dev/null || true)
            local qx_content
            qx_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "cat ${docker_dir}/qx.txt 2>/dev/null" 2>/dev/null || true)
            
            if [ -n "$vless_content" ]; then
                echo "$vless_content" > "${BATCH_TMP_DIR}/${cur_alias}.vless"
            fi
            if [ -n "$qx_content" ]; then
                echo "$qx_content" > "${BATCH_TMP_DIR}/${cur_alias}.qx"
            fi
        ) &
    done

    wait 2>/dev/null || true

    local final_done=$(find "${BATCH_TMP_DIR}" -name "*.vless" 2>/dev/null | wc -l | xargs)
    if [ "$final_done" -eq 0 ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Failed to fetch configuration from any node (make sure node is reachable and initialized)."
        else
            echo "❌ 未能从任何节点提取到配置文件 (请确保节点在线且已初始化)。"
        fi
        rm -rf "$BATCH_TMP_DIR"
        return 1
    fi

    # 聚合到临时文件
    local ALL_VLESS_FILE="${BATCH_TMP_DIR}/all.vless"
    local ALL_QX_FILE="${BATCH_TMP_DIR}/all.qx"
    : > "$ALL_VLESS_FILE"
    : > "$ALL_QX_FILE"

    for cur_alias in "${target_aliases[@]}"; do
        if [ -f "${BATCH_TMP_DIR}/${cur_alias}.vless" ]; then
            cat "${BATCH_TMP_DIR}/${cur_alias}.vless" >> "$ALL_VLESS_FILE"
            echo "" >> "$ALL_VLESS_FILE"
        fi
        if [ -f "${BATCH_TMP_DIR}/${cur_alias}.qx" ]; then
            cat "${BATCH_TMP_DIR}/${cur_alias}.qx" >> "$ALL_QX_FILE"
            echo "" >> "$ALL_QX_FILE"
        fi
    done

    # 1. 终端打印输出
    echo ""
    echo "================================================================================"
    if [ "$CNSR_LANG" = "en" ]; then
        echo "🔗 VLESS Subscriptions (Total $final_done nodes, direct copy ready):"
    else
        echo "🔗 VLESS 订阅链接聚合 (共 $final_done 条节点，可全选直接复制):"
    fi
    echo "--------------------------------------------------------------------------------"
    cat "$ALL_VLESS_FILE" | sed '/^$/d'
    echo "================================================================================"
    echo ""
    if [ "$CNSR_LANG" = "en" ]; then
        echo "🔗 Quantumult X Node Configurations (Total $final_done nodes):"
    else
        echo "🔗 Quantumult X 节点配置 (共 $final_done 条):"
    fi
    echo "--------------------------------------------------------------------------------"
    cat "$ALL_QX_FILE" | sed '/^$/d'
    echo "================================================================================"
    echo ""

    # 2. 如果携带 --tg，推送至 Telegram
    if [ "$PUSH_TG" = true ]; then
        push_links_to_tg "$ALL_VLESS_FILE" "$ALL_QX_FILE" "$final_done"
        if [ "$CNSR_LANG" = "en" ]; then
            echo "💡 Node configurations successfully pushed to Telegram (VLESS / QX blocks, total $final_done items)."
        else
            echo "💡 节点订阅配置已成功推送至 Telegram (VLESS / QX 独立代码块，共 $final_done 条)。"
        fi
    fi

    rm -rf "$BATCH_TMP_DIR"
}
