#!/bin/bash
# Snack Connoisseur - AWS Cloud Initialization Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ui.sh" ]; then source "$LIB_DIR/ui.sh"; fi
if [ -f "$LIB_DIR/ssh.sh" ]; then source "$LIB_DIR/ssh.sh"; fi

CONFIG_INPUT=""
CLI_ALIAS=""
CLI_REGION=""
CLI_ZONE=""
CLI_COUNT=""
CLI_BUNDLE=""
CLI_BLUEPRINT=""
CLI_KEY_PAIR=""
DETACH_MODE=false
DEBUG_MODE=false
EXTRA_INIT_ARGS=()
ORIGINAL_ARGS=("$@")

# 处理传参 (仅接受全称参数)
while [ $# -gt 0 ]; do
    case "$1" in
        -f|--file)
            if [ $# -ge 2 ]; then
                CONFIG_INPUT="$2"
                shift 2
            else
                shift
            fi
            ;;
        --alias)
            if [ $# -ge 2 ]; then
                CLI_ALIAS="$2"
                shift 2
            else
                shift
            fi
            ;;
        --region)
            if [ $# -ge 2 ]; then
                CLI_REGION="$2"
                shift 2
            else
                shift
            fi
            ;;
        --zone)
            if [ $# -ge 2 ]; then
                CLI_ZONE="$2"
                shift 2
            else
                shift
            fi
            ;;
        --count)
            if [ $# -ge 2 ]; then
                CLI_COUNT="$2"
                shift 2
            else
                shift
            fi
            ;;
        --bundle|--bundle-id)
            if [ $# -ge 2 ]; then
                CLI_BUNDLE="$2"
                shift 2
            else
                shift
            fi
            ;;
        --blueprint|--blueprint-id)
            if [ $# -ge 2 ]; then
                CLI_BLUEPRINT="$2"
                shift 2
            else
                shift
            fi
            ;;
        --key|--key-pair|--key-pair-name)
            if [ $# -ge 2 ]; then
                CLI_KEY_PAIR="$2"
                shift 2
            else
                shift
            fi
            ;;
        --detach|--async)
            DETACH_MODE=true
            shift
            ;;
        --harden)
            EXTRA_INIT_ARGS+=("--harden")
            shift
            ;;
        --debug)
            DEBUG_MODE=true
            EXTRA_INIT_ARGS+=("--debug")
            shift
            ;;
        *)
            if [ -z "$CLI_ALIAS" ] && [[ "$1" != -* ]]; then
                CLI_ALIAS="$1"
            fi
            shift
            ;;
    esac
done

function send_batch_tg_notify() {
    local text="$1"
    local gateway_url="${GATEWAY_URL:-}"
    local gateway_auth="${GATEWAY_AUTH_KEY:-}"
    local bot_token="${TG_BOT_TOKEN:-}"
    local chat_id="${TG_CHAT_ID:-}"

    if [ -n "$gateway_url" ]; then
        local auth_header=()
        if [ -n "$gateway_auth" ]; then
            auth_header=(-H "Authorization: Bearer $gateway_auth")
        fi
        local payload
        payload=$(python3 -c "import sys, json; print(json.dumps({'text': sys.argv[1], 'parse_mode': 'Markdown'}))" "$text" 2>/dev/null || true)
        curl -s -X POST "${auth_header[@]}" \
            -H "Content-Type: application/json" \
            "$gateway_url/api/tg" \
            -d "$payload" >/dev/null 2>&1 || true
        return 0
    fi

    if [ -n "$bot_token" ] && [ -n "$chat_id" ]; then
        curl -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
            -d chat_id="${chat_id}" \
            -d parse_mode="Markdown" \
            -d text="${text}" >/dev/null 2>&1 || true
    fi
}

# 0. 脱机后台执行分支 (Detach Mode)
if [ "$DETACH_MODE" = "true" ] && [ -z "$CNSR_DETACHED_CHILD" ]; then
    mkdir -p "logs"
    LOG_TAG="${CLI_ALIAS:-${CONFIG_INPUT:-aws_batch}}"
    LOG_TAG=$(echo "$LOG_TAG" | tr -cd 'a-zA-Z0-9_-')
    LOG_FILE="logs/deploy_${LOG_TAG}_$(date +%Y%m%d_%H%M%S).log"
    
    # 过滤掉 --detach / --async 传给后台子进程
    FILTERED_ARGS=()
    for arg in "${ORIGINAL_ARGS[@]}"; do
        if [[ "$arg" != "--detach" && "$arg" != "--async" ]]; then
            FILTERED_ARGS+=("$arg")
        fi
    done

    export CNSR_DETACHED_CHILD=1
    nohup "$0" "${FILTERED_ARGS[@]}" > "$LOG_FILE" 2>&1 &
    BG_PID=$!

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🚀 Batch deployment launched in background (PID: $BG_PID)."
        echo "📄 Real-time logs: $LOG_FILE"
        echo "💡 You can safely close this terminal. Telegram alerts will track stage progress."
        echo "🔍 To view progress: tail -f $LOG_FILE"
    else
        echo "🚀 批量部署已转入后台脱机运行 (进程 PID: $BG_PID)。"
        echo "📄 实时日志输出于: $LOG_FILE"
        echo "💡 您现在可以安全关闭终端窗口，部署阶段进度与节点凭据将实时推送到 Telegram。"
        echo "🔍 查看实时进度命令: tail -f $LOG_FILE"
    fi

    send_batch_tg_notify "🚀 *[Snack] AWS 批量部署已启动 (后台脱机模式)*
• *预设/目标*: \`${LOG_TAG}\`
• *后台 PID*: \`${BG_PID}\`
• *日志路径*: \`${LOG_FILE}\`
💡 您可安全关闭终端，各节点将自治完成配置并推送凭据。"

    exit 0
fi

# 1. pre-flight: uv 是否存在，.env 是否含 AWS AK/SK
if ! command -v uv >/dev/null 2>&1; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: uv not found. Please install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
        echo "❌ 错误: 未找到 uv。请先安装 uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
    exit 1
fi

if [ ! -f ".env" ] || ! grep -q "AWS_ACCESS_KEY_ID" .env || ! grep -q "AWS_SECRET_ACCESS_KEY" .env; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: Missing AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY in .env"
    else
        echo "❌ 错误: .env 中缺少 AWS_ACCESS_KEY_ID 或 AWS_SECRET_ACCESS_KEY"
    fi
    exit 1
fi
# 确保在子进程 (后台模式) 中也能拿到环境变量
set -a
source .env
set +a

function provision_single_instance() {
    local inst_alias="$1"
    local inst_region="$2"
    local inst_bundle="$3"
    local inst_blueprint="$4"
    local inst_key_pair="$5"
    local inst_zone="${6:-}"
    local inst_user="${7:-}"
    local inst_key_file="${8:-}"
    local inst_key_name="${9:-}"

    export NON_INTERACTIVE=true
    local extra_py_args=()
    if [ -n "$inst_key_pair" ]; then
        extra_py_args+=(--key-pair "$inst_key_pair")
    fi
    if [ -n "$inst_zone" ]; then
        extra_py_args+=(--zone "$inst_zone")
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "☁️ [$inst_alias] Calling AWS API to create instance in $inst_region ($inst_bundle, $inst_blueprint)..."
    else
        echo "☁️ [$inst_alias] 正在通过 AWS API 创建实例 (区域: $inst_region, 规格: $inst_bundle, 镜像: $inst_blueprint)..."
    fi
    
    set +e
    local output
    output=$(uv run providers/aws.py create --alias "$inst_alias" --region "$inst_region" --count 1 --bundle "$inst_bundle" --blueprint "$inst_blueprint" "${extra_py_args[@]}" 2>&1)
    local status=$?
    set -e
    
    if [ $status -ne 0 ]; then
        echo "❌ 错误: AWS 实例创建失败 (AWS instance creation failed) [$inst_alias]"
        echo "$output"
        send_batch_tg_notify "🚨 *[Snack 告警] 实例创建失败*
• *节点别名*: \`${inst_alias}\`
• *目标区域*: \`${inst_region}\`
• *错误详情*:
\`\`\`
${output}
\`\`\`"
        return 1
    fi
    
    # Parse JSON directly in Python to extract IP
    local ip
    ip=$(echo "$output" | uv run python3 -c "
import sys, json
try:
    for line in sys.stdin:
        line = line.strip()
        if line.startswith('{'):
            data = json.loads(line)
            if 'ip' in data:
                print(data['ip'])
                break
except Exception:
    pass
")
    
    if [ -z "$ip" ]; then
        echo "❌ 错误: 未能获取到新实例的 IP (Failed to get IP for new instance) [$inst_alias]"
        echo "Output was: $output"
        return 1
    fi
    
    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ [$inst_alias] Instance created! IP: $ip. Waiting 20s for sshd to start..."
    else
        echo "✅ [$inst_alias] 实例创建成功，IP: $ip，等待云端 SSH 启动 (20秒)..."
    fi

    send_batch_tg_notify "☁️ *[Snack] 实例创建就绪*
• *节点别名*: \`${inst_alias}\`
• *分配 IP*: \`${ip}\`
• *区域*: \`${inst_region}\`
⚙️ 正在连接 SSH 装配 Docker 与 X-ray 基础环境..."

    sleep 20
    
    # 移交部署 (在子 shell 中独立执行，完全隔离环境)
    echo "🚀 [$inst_alias] 开始移交至 core/init.sh 静默部署..."
    if [ -f "$SCRIPT_DIR/init.sh" ]; then
        (
            source "$SCRIPT_DIR/init.sh"
            SSH_ALIAS="$inst_alias"
            TARGET_IP="$ip"
            export NON_INTERACTIVE=true
            local init_pass_args=(--non-interactive)
            if [ -n "$inst_user" ]; then init_pass_args+=(-u "$inst_user"); fi
            if [ -n "$inst_key_file" ]; then init_pass_args+=(--identity-file "$inst_key_file"); fi
            if [ -n "$inst_key_name" ]; then init_pass_args+=(--key "$inst_key_name"); fi
            module_init "${init_pass_args[@]}" "${EXTRA_INIT_ARGS[@]}"
        )
    else
        echo "❌ 错误: 找不到 core/init.sh (core/init.sh not found)"
        return 1
    fi
}

function provision_batch_group() {
    local grp_alias="$1"
    local grp_region="$2"
    local grp_count="${3:-1}"
    local grp_bundle="${4:-nano_3_0}"
    local grp_blueprint="${5:-debian_12}"
    local grp_key_pair="${6:-}"
    local grp_zone="${7:-}"

    # 数量安全上限拦截 (最大 20 台，保护 GitHub Actions 配额)
    if [ "$grp_count" -gt 20 ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Batch count ($grp_count) exceeds safety limit of 20 instances (to avoid GitHub Actions quota exhaustion)."
        else
            echo "❌ 错误: 单批次部署数量 ($grp_count) 超过安全限制 (最多 20 台)，防止 GitHub Actions 扫描配额超限与云 API 速率惩罚。"
        fi
        exit 1
    fi

    # 防重检查: 遍历 count，检测 ~/.ssh/config 冲突
    for ((i=1; i<=grp_count; i++)); do
        local cur_alias="${grp_alias}"
        if [ "$grp_count" -gt 1 ]; then
            cur_alias="${grp_alias}-${i}"
        fi
        if grep -q -E "^Host $cur_alias$" ~/.ssh/config 2>/dev/null; then
            if [ "$CNSR_LANG" = "en" ]; then
                echo "❌ Error: Alias $cur_alias already exists in ~/.ssh/config. Please clean up to avoid conflicts."
            else
                echo "❌ 错误: ~/.ssh/config 中已存在别名 $cur_alias，请清理或更换别名防止冲突。"
            fi
            return 1
        fi
    done

    # 集中预解析用户名与批量统一密钥
    local RESOLVED_USER=""
    if [ -n "$grp_blueprint" ]; then
        RESOLVED_USER=$(blueprint_to_default_user "$grp_blueprint")
    else
        RESOLVED_USER="admin"
    fi

    local RESOLVED_KEY="$grp_key_pair"
    local RESOLVED_KEY_FILE=""
    
    mkdir -p "$HOME/.ssh/pub"
    local agent_keys=()
    while IFS= read -r line; do
        [ -n "$line" ] && agent_keys+=("$line")
    done < <(ssh-add -L 2>/dev/null || true)

    if [ -n "$RESOLVED_KEY" ]; then
        local matched_agent_key=""
        for k in "${agent_keys[@]}"; do
            local comment=$(echo "$k" | awk '{print $3}')
            if [ "$comment" = "$RESOLVED_KEY" ]; then
                matched_agent_key="$k"
                break
            fi
        done
        if [ -n "$matched_agent_key" ]; then
            local BATCH_KEY_PUB="$HOME/.ssh/pub/${RESOLVED_KEY}.pub"
            echo "$matched_agent_key" > "$BATCH_KEY_PUB"
            RESOLVED_KEY_FILE="$BATCH_KEY_PUB"
        elif [ -f "$HOME/.ssh/${RESOLVED_KEY}" ]; then
            RESOLVED_KEY_FILE="$HOME/.ssh/${RESOLVED_KEY}"
        elif [ -f "$HOME/.ssh/${RESOLVED_KEY}.pem" ]; then
            RESOLVED_KEY_FILE="$HOME/.ssh/${RESOLVED_KEY}.pem"
        fi
    fi

    if [ -z "$RESOLVED_KEY_FILE" ] && [ ${#agent_keys[@]} -gt 1 ] && [ -t 0 ] && [ "$CNSR_DETACHED_CHILD" != "1" ]; then
        local options=()
        for k in "${agent_keys[@]}"; do
            local comment=$(echo "$k" | awk '{print $3}')
            options+=("$comment")
        done
        select_menu "📋 检测到批量部署任务 (共 $grp_count 台)，请统一选择本批次使用的 SSH Agent 密钥:" "${options[@]}"
        local sel_idx=$((MENU_CHOICE - 1))
        RESOLVED_KEY="${options[$sel_idx]}"
        local BATCH_KEY_PUB="$HOME/.ssh/pub/${RESOLVED_KEY}.pub"
        echo "${agent_keys[$sel_idx]}" > "$BATCH_KEY_PUB"
        RESOLVED_KEY_FILE="$BATCH_KEY_PUB"
        echo "   🎯 批量部署已统一锁定密钥: $RESOLVED_KEY"
    elif [ -z "$RESOLVED_KEY_FILE" ] && [ ${#agent_keys[@]} -ge 1 ]; then
        RESOLVED_KEY="${agent_keys[0]##* }"
        local BATCH_KEY_PUB="$HOME/.ssh/pub/${RESOLVED_KEY}.pub"
        echo "${agent_keys[0]}" > "$BATCH_KEY_PUB"
        RESOLVED_KEY_FILE="$BATCH_KEY_PUB"
    fi

    # Stage 1: 批量开机通知 (清晰独立字段)
    if [ "$CNSR_DETACHED_CHILD" != "1" ]; then
        local tpl_name="${CONFIG_INPUT:-$grp_alias}"
        send_batch_tg_notify "🚀 *[Snack] AWS 批量编排任务启动*
• *任务模版*: \`${tpl_name}\`
• *节点数量*: \`${grp_count}\` 台 (\`${grp_alias}-1\` ~ \`${grp_count}\`)
• *目标区域*: \`${grp_region}\`
• *规格套餐*: \`${grp_bundle}\`
• *系统镜像*: \`${grp_blueprint}\`"
    fi

    local BATCH_TMP_DIR=$(mktemp -d)

    # Phase 1: 并发调用 AWS API 创建实例并收集 IP 与 Zone
    if [ "$DEBUG_MODE" = "true" ]; then
        echo "☁️ 正在批量调用 AWS API 创建 $grp_count 台实例..."
    fi
    local start_time_p1=$(date +%s)
    for ((i=1; i<=grp_count; i++)); do
        local current_alias="${grp_alias}"
        if [ "$grp_count" -gt 1 ]; then
            current_alias="${grp_alias}-${i}"
        fi
        
        (
            if [ "$DEBUG_MODE" != "true" ]; then
                exec > "logs/init_aws_${current_alias}.log" 2>&1
            fi
            
            local extra_py_args=()
            if [ -n "$grp_key_pair" ]; then extra_py_args+=(--key-pair "$grp_key_pair"); fi
            if [ -n "$grp_zone" ]; then extra_py_args+=(--zone "$grp_zone"); fi
            
            echo "☁️ [$current_alias] 正在通过 AWS API 创建实例 (区域: $grp_region, 规格: $grp_bundle, 镜像: $grp_blueprint)..."
            local py_out
            py_out=$(uv run providers/aws.py create --alias "$current_alias" --region "$grp_region" --count 1 --bundle "$grp_bundle" --blueprint "$grp_blueprint" "${extra_py_args[@]}" 2>&1)
            local status=$?
            
            if [ $status -ne 0 ]; then
                echo "❌ 错误: AWS 实例创建失败 [$current_alias]"
                echo "$py_out"
                echo "err" > "${BATCH_TMP_DIR}/${current_alias}_aws.err"
            else
                uv run python3 -c "
import sys, json
try:
    for line in sys.stdin:
        line = line.strip()
        if line.startswith('{'):
            data = json.loads(line)
            if 'ip' in data and data['ip']:
                with open('${BATCH_TMP_DIR}/${current_alias}.json', 'w') as f:
                    json.dump(data, f)
                break
except Exception:
    pass
" <<< "$py_out"
                if [ ! -f "${BATCH_TMP_DIR}/${current_alias}.json" ]; then
                    echo "err" > "${BATCH_TMP_DIR}/${current_alias}_aws.err"
                fi
            fi
        ) &
    done

    if [ "$DEBUG_MODE" != "true" ]; then
        while true; do
            local p1_done=$(ls -1 "$BATCH_TMP_DIR"/*.json 2>/dev/null | wc -l | xargs)
            local p1_err=$(ls -1 "$BATCH_TMP_DIR"/*_aws.err 2>/dev/null | wc -l | xargs)
            local p1_total_finished=$((p1_done + p1_err))
            
            render_bar_line "$p1_total_finished" "$grp_count" "实例创建与公网 IP 分配中..." 32
            if [ "$p1_total_finished" -ge "$grp_count" ]; then
                break
            fi
            sleep 0.5
        done
        local end_time_p1=$(date +%s)
        local elapsed_p1=$((end_time_p1 - start_time_p1))
        render_bar_done "1/2" "AWS 实例创建与多可用区 IP 就绪" "$p1_done" "$elapsed_p1"
    else
        wait
    fi

    local created_count=$(ls -1 "$BATCH_TMP_DIR"/*.json 2>/dev/null | wc -l | xargs)
    if [ "$created_count" -eq 0 ]; then
        echo "❌ 错误: 批量创建失败，未获取到任何有效实例信息。"
        send_batch_tg_notify "🚨 *[Snack 告警] AWS 批量创建失败*
• *目标区域*: \`${grp_region}\`
• *错误详情*: 所有实例创建均未返回有效 IP。"
        rm -rf "$BATCH_TMP_DIR"
        return 1
    fi

    # 生成 AZ 统计与实例清单
    local SUMMARY_JSON
    SUMMARY_JSON=$(uv run python3 -c "
import glob, json, os
files = sorted(glob.glob('${BATCH_TMP_DIR}/*.json'))
nodes = []
az_counts = {}
for f in files:
    try:
        with open(f) as fp:
            d = json.load(fp)
            nodes.append(d)
            z = d.get('zone', '')
            if z:
                az_counts[z] = az_counts.get(z, 0) + 1
    except Exception:
        pass

print(json.dumps({'nodes': nodes, 'az_counts': az_counts}))
")

    local AZ_TEXT
    AZ_TEXT=$(uv run python3 -c "
import json, sys
data = json.loads('''$SUMMARY_JSON''')
az_counts = data.get('az_counts', {})
if az_counts:
    for z, c in sorted(az_counts.items()):
        print(f'  - \`{z}\`: {c} 台')
else:
    print('  - 默认可用区')
")

    local NODES_TEXT
    NODES_TEXT=$(uv run python3 -c "
import json, sys
data = json.loads('''$SUMMARY_JSON''')
nodes = data.get('nodes', [])
for n in nodes:
    name = n.get('name', '')
    ip = n.get('ip', '')
    z = n.get('zone', '')
    z_suffix = f' (\`{z.split(\"-\")[-1]}\`)' if z else ''
    print(f'• \`{name}\`: \`{ip}\`{z_suffix}')
")

    # Stage 2: 统一汇总就绪看板
    send_batch_tg_notify "☁️ *[Snack] AWS 批量实例就绪看板* ($created_count/$grp_count)
• *目标区域*: \`${grp_region}\`
• *可用区分布统计*:
$AZ_TEXT

📋 *节点与 IP 分配详情*:
$NODES_TEXT

⚙️ 正在并行连接各节点装配基础环境与 X-ray 服务..."

    # Phase 2: 并行向各机器装配基础环境 (core/init.sh)
    if [ "$DEBUG_MODE" = "true" ]; then
        echo "⚙️ 正在并行向 $created_count 台机器装配基础环境与组件..."
    fi
    local start_time_p2=$(date +%s)
    for ((i=1; i<=grp_count; i++)); do
        local current_alias="${grp_alias}"
        if [ "$grp_count" -gt 1 ]; then
            current_alias="${grp_alias}-${i}"
        fi
        
        local info_file="${BATCH_TMP_DIR}/${current_alias}.json"
        if [ -f "$info_file" ]; then
            local current_ip=$(uv run python3 -c "import json; print(json.load(open('$info_file')).get('ip', ''))")
            (
                if [ "$DEBUG_MODE" != "true" ]; then
                    exec > "logs/init_worker_${current_alias}.log" 2>&1
                fi
                source "$SCRIPT_DIR/init.sh"
                SSH_ALIAS="$current_alias"
                TARGET_IP="$current_ip"
                export NON_INTERACTIVE=true
                local init_pass_args=(--non-interactive)
                if [ -n "$RESOLVED_USER" ]; then init_pass_args+=(-u "$RESOLVED_USER"); fi
                if [ -n "$RESOLVED_KEY_FILE" ]; then init_pass_args+=(--identity-file "$RESOLVED_KEY_FILE"); fi
                if [ -n "$RESOLVED_KEY" ]; then init_pass_args+=(--key "$RESOLVED_KEY"); fi
                if module_init "${init_pass_args[@]}" "${EXTRA_INIT_ARGS[@]}"; then
                    echo "done" > "${BATCH_TMP_DIR}/${current_alias}.done"
                else
                    echo "err" > "${BATCH_TMP_DIR}/${current_alias}.err"
                fi
            ) &
        fi
    done

    if [ "$DEBUG_MODE" != "true" ]; then
        while true; do
            local p2_done=$(ls -1 "$BATCH_TMP_DIR"/*.done 2>/dev/null | wc -l | xargs)
            local p2_err=$(ls -1 "$BATCH_TMP_DIR"/*.err 2>/dev/null | wc -l | xargs)
            local p2_total_finished=$((p2_done + p2_err))
            
            render_bar_line "$p2_total_finished" "$created_count" "节点基础环境装配中..." 32
            if [ "$p2_total_finished" -ge "$created_count" ]; then
                break
            fi
            sleep 0.5
        done
        local end_time_p2=$(date +%s)
        local elapsed_p2=$((end_time_p2 - start_time_p2))
        render_bar_done "2/2" "节点基础环境装配完成" "$p2_done" "$elapsed_p2"
        echo ""
        
        if [ "$p2_err" -gt 0 ]; then
            for err_file in "$BATCH_TMP_DIR"/*.err; do
                [ -f "$err_file" ] || continue
                local err_alias=$(basename "$err_file" .err)
                echo "⚠️ 节点 ${err_alias} 装配失败，排查日志: logs/init_worker_${err_alias}.log"
            done
        fi
    else
        wait
    fi

    local done_count=$(ls -1 "$BATCH_TMP_DIR"/*.done 2>/dev/null | wc -l | xargs)

    # Stage 3: 最终完工总览
    send_batch_tg_notify "🎉 *[Snack] AWS 批量部署全部完成* ($done_count/$grp_count)
• *任务模版*: \`${CONFIG_INPUT:-$grp_alias}\`
• *部署结果*: $done_count 台全部成功
• *目标区域*: \`${grp_region}\`
• *本地凭据*: 已全部写入 \`~/.ssh/config\`
• *云端体检*: GitHub Actions 扫描已全量触发
💡 节点集群已自治进入 15 分钟自动化轮换与自愈生命周期。"

    rm -rf "$BATCH_TMP_DIR"
}

# 模版智能寻路解析
if [ -n "$CONFIG_INPUT" ]; then
    RESOLVED_CONF=""
    if [ -f "$CONFIG_INPUT" ]; then
        RESOLVED_CONF="$CONFIG_INPUT"
    elif [ -f "templates/${CONFIG_INPUT}" ]; then
        RESOLVED_CONF="templates/${CONFIG_INPUT}"
    elif [ -f "templates/${CONFIG_INPUT}.conf" ]; then
        RESOLVED_CONF="templates/${CONFIG_INPUT}.conf"
    elif [ -f "templates/${CONFIG_INPUT}_nodes.conf" ]; then
        RESOLVED_CONF="templates/${CONFIG_INPUT}_nodes.conf"
    fi

    if [ -z "$RESOLVED_CONF" ] || [ ! -f "$RESOLVED_CONF" ]; then
        echo "❌ 错误: 找不到指定的模版文件 (Template file not found): $CONFIG_INPUT"
        exit 1
    fi

    echo "📄 正在加载模版预设 (Loading profile): $RESOLVED_CONF"

    if grep -q -E "^[[:space:]]*(REGION|ZONE|ALIAS|BUNDLE|BLUEPRINT|KEY_PAIR|KEY)=" "$RESOLVED_CONF"; then
        # 默认值
        T_REGION=""
        T_ZONE=""
        T_ALIAS=""
        T_BUNDLE="nano_3_0"
        T_BLUEPRINT="debian_12"
        T_KEY_PAIR=""
        T_COUNT=1

        # 读取模版变量
        while IFS='=' read -r key val || [ -n "$key" ]; do
            key=$(echo "$key" | tr -d '[:space:]')
            val=$(echo "$val" | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d '\r')
            case "$key" in
                REGION) T_REGION="$val" ;;
                ZONE) T_ZONE="$val" ;;
                ALIAS) T_ALIAS="$val" ;;
                BUNDLE) T_BUNDLE="$val" ;;
                BLUEPRINT) T_BLUEPRINT="$val" ;;
                KEY_PAIR|KEY) T_KEY_PAIR="$val" ;;
                COUNT) T_COUNT="$val" ;;
            esac
        done < "$RESOLVED_CONF"

        # CLI 参数具有更高优先级，覆盖模版默认值
        FINAL_REGION="${CLI_REGION:-$T_REGION}"
        FINAL_ZONE="${CLI_ZONE:-$T_ZONE}"
        FINAL_ALIAS="${CLI_ALIAS:-$T_ALIAS}"
        FINAL_COUNT="${CLI_COUNT:-$T_COUNT}"
        FINAL_BUNDLE="${CLI_BUNDLE:-$T_BUNDLE}"
        FINAL_BLUEPRINT="${CLI_BLUEPRINT:-$T_BLUEPRINT}"
        FINAL_KEY_PAIR="${CLI_KEY_PAIR:-$T_KEY_PAIR}"

        if [ -z "$FINAL_ALIAS" ] || [ -z "$FINAL_REGION" ]; then
            echo "❌ 错误: 模版中必须指定 ALIAS 和 REGION。"
            exit 1
        fi

        provision_batch_group "$FINAL_ALIAS" "$FINAL_REGION" "$FINAL_COUNT" "$FINAL_BUNDLE" "$FINAL_BLUEPRINT" "$FINAL_KEY_PAIR" "$FINAL_ZONE"
        echo "✅ 模版实例编排完成 (Provisioning complete)."
        exit 0
    else
        # 兼容多行列表模式
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
            [[ -z "$line" || "$line" == \#* ]] && continue

            read -r f_alias f_region f_count f_bundle f_blueprint <<< "$line"
            f_count="${f_count:-1}"
            f_bundle="${f_bundle:-nano_3_0}"
            f_blueprint="${f_blueprint:-debian_12}"

            if [ -n "$f_alias" ] && [ -n "$f_region" ]; then
                provision_batch_group "$f_alias" "$f_region" "$f_count" "$f_bundle" "$f_blueprint" "$CLI_KEY_PAIR" "$CLI_ZONE"
            fi
        done < "$RESOLVED_CONF"

        echo "✅ 清单文件中所有 AWS 实例编排完成。"
        exit 0
    fi
fi

# 模式 2: 单组 CLI 参数执行
FINAL_ALIAS="$CLI_ALIAS"
FINAL_REGION="$CLI_REGION"
FINAL_ZONE="$CLI_ZONE"
FINAL_COUNT="${CLI_COUNT:-1}"
FINAL_BUNDLE="${CLI_BUNDLE:-nano_3_0}"
FINAL_BLUEPRINT="${CLI_BLUEPRINT:-debian_12}"
FINAL_KEY_PAIR="$CLI_KEY_PAIR"

if [ -z "$FINAL_ALIAS" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: Please specify node alias or template profile. (Usage: ./cnsr.sh init-aws -f jp_aws-lightsail [--count 3] OR ./cnsr.sh init-aws <alias> --region <region>)"
    else
        echo "❌ 错误: 请指定节点基础别名或模版预设。 (用法: ./cnsr.sh init-aws -f jp_aws-lightsail [--count 3] 或 ./cnsr.sh init-aws <别名> --region <区域>)"
    fi
    exit 1
fi

if [ -z "$FINAL_REGION" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: AWS mode requires --region. (e.g., --region ap-northeast-1)"
    else
        echo "❌ 错误: AWS 模式需要指定 --region。 (例如: --region ap-northeast-1)"
    fi
    exit 1
fi

provision_batch_group "$FINAL_ALIAS" "$FINAL_REGION" "$FINAL_COUNT" "$FINAL_BUNDLE" "$FINAL_BLUEPRINT" "$FINAL_KEY_PAIR" "$FINAL_ZONE"
echo "✅ 所有实例编排完成 (All AWS instances provisioned and initialized)."
exit 0



