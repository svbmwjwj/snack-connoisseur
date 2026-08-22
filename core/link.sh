#!/bin/bash
# Snack Connoisseur - Get Links Module
# Fetches vless and qx configurations from remote nodes

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

function push_links_to_tg() {
    local vless_file="$1"
    local qx_file="$2"
    local count="$3"
    local tmp_dir="$4"
    local aliases_json="$5"

    uv run python3 -c "
import os, sys, json, urllib.request, urllib.parse

vless_file = sys.argv[1]
qx_file = sys.argv[2]
count = sys.argv[3]
tmp_dir = sys.argv[4]
target_aliases = json.loads(sys.argv[5])

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

def get_health_summary():
    anomalies = []
    normal_count = 0
    total = len(target_aliases)
    for alias in target_aliases:
        h_file = os.path.join(tmp_dir, f'{alias}.health')
        if os.path.exists(h_file):
            try:
                with open(h_file, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                    sni = d.get('sni', 'N/A')
                    conclusion = d.get('conclusion', '🟢 Normal')
                    
                    if '🔴' in conclusion:
                        fail_text = conclusion.replace('🔴', '').strip()
                        anomalies.append(f'🔴 \`{alias}\`: {fail_text} (SNI: \`{sni}\`)')
                    elif '🟡' in conclusion:
                        warn_text = conclusion.replace('🟡', '').strip()
                        anomalies.append(f'🟡 \`{alias}\`: {warn_text} (SNI: \`{sni}\`)')
                    else:
                        normal_count += 1
            except Exception:
                anomalies.append(f'🔴 \`{alias}\`: 体检报告解析异常')
        else:
            anomalies.append(f'🔴 \`{alias}\`: 未能获取体检快照 (待就绪/超时)')

    if len(anomalies) == 0:
        return f'📊 *AWS 集群体检*: ✅ {normal_count}/{total} 节点全部运行健康 (网络/容器/SNI 均无异常)'
    else:
        txt = f'📊 *AWS 集群体检*: ⚠️ {normal_count}/{total} 节点健康，{len(anomalies)} 台存在异常\n• *异常详情*:\n'
        txt += '\n'.join([f'  {a}' for a in anomalies])
        return txt

def push_block(title, filepath, app_hint):
    if not os.path.exists(filepath):
        return
        
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]
        
    if not lines:
        return
    
    # 按照每组最多 15 个节点分片推送，充分利用 4096 字符限制并避免超载
    chunk_size = 15
    total_chunks = (len(lines) + chunk_size - 1) // chunk_size
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i+chunk_size]
        chunk_idx = i // chunk_size + 1
        part_suffix = f' (分片 {chunk_idx}/{total_chunks})' if total_chunks > 1 else ''
        content = '\n'.join(chunk)
        
        msg = f'{title}{part_suffix} (共 {len(lines)} 条)\n\`\`\`text\n{content}\n\`\`\`\n💡 *适用于*: {app_hint}'
        send_tg(msg)

# 1. 发送独立体检看板
health_text = get_health_summary()
if health_text:
    send_tg(health_text)

# 2. 发送 VLESS 链接
push_block('🔗 *VLESS 订阅链接聚合*', vless_file, 'V2rayN / v2rayA / Shadowrocket')

# 3. 发送 QX 链接
push_block('🔗 *Quantumult X 节点配置*', qx_file, 'Quantumult X [server_local]')
" "$vless_file" "$qx_file" "$count" "$tmp_dir" "$aliases_json"
}

function module_link() {
    if [ $# -eq 0 ]; then
        echo "❌ 错误: 请指定节点别名或匹配模式 (例如: jp_aws-lightsail-1 或 jp_aws-lightsail-*)"
        return 1
    fi

    local target_aliases=()
    for arg in "$@"; do
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

    # 去重并排序
    if [ ${#target_aliases[@]} -gt 0 ]; then
        IFS=$'\n' target_aliases=($(sort -V -u <<<"${target_aliases[*]}"))
        unset IFS
    fi

    if [ ${#target_aliases[@]} -eq 0 ]; then
        echo "❌ 未找到任何有效的目标节点。"
        return 1
    fi

    local count=${#target_aliases[@]}
    echo "📡 正在从 $count 个节点提取订阅配置与体检报告..."
    
    local BATCH_TMP_DIR=$(mktemp -d)

    # 并发异步抓取 (各节点每 2s 探测一次，就绪即取)
    for cur_alias in "${target_aliases[@]}"; do
        (
            local remote_home
            remote_home=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
            local docker_dir="${remote_home}/docker-apps/xray"
            
            local poll_count=0
            while [ $poll_count -lt 20 ]; do
                local vless_content
                vless_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "cat ${docker_dir}/vless.txt 2>/dev/null" 2>/dev/null || true)
                local qx_content
                qx_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "cat ${docker_dir}/qx.txt 2>/dev/null" 2>/dev/null || true)
                local health_content
                health_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$cur_alias" "cat ${docker_dir}/health.json 2>/dev/null" 2>/dev/null || true)
                
                if [ -n "$vless_content" ] && [ -n "$qx_content" ]; then
                    echo "$vless_content" > "${BATCH_TMP_DIR}/${cur_alias}.vless"
                    echo "$qx_content" > "${BATCH_TMP_DIR}/${cur_alias}.qx"
                    [ -n "$health_content" ] && echo "$health_content" > "${BATCH_TMP_DIR}/${cur_alias}.health"
                    break
                fi
                sleep 2
                poll_count=$((poll_count + 1))
            done
        ) &
    done
    
    local total=$count
    local start_time=$(date +%s)
    while true; do
        local current_done=$(find "${BATCH_TMP_DIR}" -name "*.vless" 2>/dev/null | wc -l | xargs)
        local now=$(date +%s)
        local elapsed=$((now - start_time))
        render_bar_line "$current_done" "$total" "提取配置与体检中..." 32 "$elapsed"
        if [ "$current_done" -ge "$total" ]; then
            break
        fi
        if [ -z "$(jobs -p)" ]; then
            break
        fi
        if [ "$elapsed" -ge 45 ]; then
            break
        fi
        sleep 0.2
    done
    
    local active_jobs=$(jobs -p)
    if [ -n "$active_jobs" ]; then
        kill $active_jobs 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    
    local end_time=$(date +%s)
    local elapsed=$((end_time - start_time))
    local final_done=$(find "${BATCH_TMP_DIR}" -name "*.vless" 2>/dev/null | wc -l | xargs)
    render_bar_done "✔" "节点配置与体检提取完成" "$final_done" "$total" "$elapsed"
    echo ""

    if [ "$final_done" -eq 0 ]; then
        echo "❌ 未能从任何节点提取到配置文件 (请确保节点在线且已初始化)。"
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

    local ALIASES_JSON
    ALIASES_JSON=$(python3 -c "import sys, json; print(json.dumps(sys.argv[1:]))" "${target_aliases[@]}")

    # 拆分推送到 Telegram (包含健康汇总 + VLESS + QX)
    push_links_to_tg "$ALL_VLESS_FILE" "$ALL_QX_FILE" "$final_done" "$BATCH_TMP_DIR" "$ALIASES_JSON"
    echo "💡 节点健康体检汇总与订阅配置已成功推送至 Telegram (健康看板 / VLESS / QX 专条，共 $final_done 条)。"

    rm -rf "$BATCH_TMP_DIR"
}
