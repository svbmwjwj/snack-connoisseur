#!/bin/bash
# Snack Connoisseur - Node Initialization Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ui.sh" ]; then source "$LIB_DIR/ui.sh"; fi
if [ -f "$LIB_DIR/ssh.sh" ]; then source "$LIB_DIR/ssh.sh"; fi
if [ -f "$LIB_DIR/security.sh" ]; then source "$LIB_DIR/security.sh"; fi
if [ -f "$SCRIPT_DIR/rotate.sh" ]; then source "$SCRIPT_DIR/rotate.sh"; fi
if [ -f "$SCRIPT_DIR/update.sh" ]; then source "$SCRIPT_DIR/update.sh"; fi

function module_init() {
    local SECURE_MODE="${HARDEN_MODE:-false}"
    local DEBUG_MODE="${DEBUG_MODE:-false}"
    local EXPLICIT_USER=""
    local EXPLICIT_KEY=""
    local EXPLICIT_IDENTITY_FILE=""
    local NON_INTERACTIVE_MODE="${NON_INTERACTIVE:-false}"
    
    while [ $# -gt 0 ]; do
        case "$1" in
            --harden|-harden)
                SECURE_MODE="true"
                shift
                ;;
            --debug|-debug)
                DEBUG_MODE="true"
                shift
                ;;
            -u|--user)
                EXPLICIT_USER="$2"
                shift 2
                ;;
            --key|--key-name)
                EXPLICIT_KEY="$2"
                shift 2
                ;;
            -i|--identity-file)
                EXPLICIT_IDENTITY_FILE="$2"
                shift 2
                ;;
            --non-interactive)
                NON_INTERACTIVE_MODE="true"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    # 兼容原逻辑，如果 TARGET_IP 原本是 --harden 或者是 --debug，则清空
    if [[ "$TARGET_IP" == --* ]]; then
        TARGET_IP=""
    fi

    echo "🚀 开始初始化新节点: $SSH_ALIAS"

    local DETECTED_USER=""
    if [ -n "$TARGET_IP" ]; then
        if [ -n "$EXPLICIT_USER" ]; then
            DETECTED_USER="$EXPLICIT_USER"
        else
            # 尝试智能 SSH 盲探 (Dynamic Probe)
            local PROBED_USER=""
            if [ -n "$EXPLICIT_IDENTITY_FILE" ]; then
                PROBED_USER=$(probe_ssh_user "$TARGET_IP" "$EXPLICIT_IDENTITY_FILE" 2>/dev/null || true)
            else
                PROBED_USER=$(probe_ssh_user "$TARGET_IP" 2>/dev/null || true)
            fi
            
            if [ -n "$PROBED_USER" ]; then
                DETECTED_USER="$PROBED_USER"
                if [ "$CNSR_LANG" = "en" ]; then
                    echo "   🎯 Smart detected initial username: $DETECTED_USER"
                else
                    echo "   🎯 智能感应到初始用户名: $DETECTED_USER"
                fi
            elif [ "$NON_INTERACTIVE_MODE" = "true" ] || [ ! -t 0 ]; then
                DETECTED_USER=$(guess_default_user "$SSH_ALIAS")
            else
                local SUGGESTED_USER=$(guess_default_user "$SSH_ALIAS")
                local INPUT_USER=""
                if [ "$CNSR_LANG" = "en" ]; then
                    echo "💡 Detected alias [$SSH_ALIAS], inferred default username: $SUGGESTED_USER"
                    read -p "👤 Enter initial username [Default: $SUGGESTED_USER] (Press Enter to confirm): " INPUT_USER
                else
                    echo "💡 检测到别名 [$SSH_ALIAS]，推测默认初始用户名: $SUGGESTED_USER"
                    read -p "👤 请输入初始用户名 [默认: $SUGGESTED_USER] (直接回车确认): " INPUT_USER
                fi
                INPUT_USER=$(echo "$INPUT_USER" | tr -d '\r\n\t' | xargs)
                DETECTED_USER="${INPUT_USER:-$SUGGESTED_USER}"
            fi
        fi
        
        if [ "$CNSR_LANG" = "en" ]; then
            echo "   🎯 Using initial username: $DETECTED_USER"
        else
            echo "   🎯 已采用初始用户名: $DETECTED_USER"
        fi
        
        # 自动清除本地 possible MITM 残留（如果这个 IP 以前被用过）
        ssh-keygen -R "$TARGET_IP" >/dev/null 2>&1 || true

        local FINAL_IDENTITY_FILE=""
        local FINAL_PUB_KEY=""

        if [ "$NON_INTERACTIVE_MODE" = "true" ] || [ ! -t 0 ]; then
            # 静默模式 - 全自动解析密钥
            mkdir -p "$HOME/.ssh/pub"
            local AGENT_PUB_FILE="$HOME/.ssh/pub/${SSH_ALIAS}.pub"
            
            if [ -n "$EXPLICIT_IDENTITY_FILE" ] && [ -f "$EXPLICIT_IDENTITY_FILE" ]; then
                FINAL_IDENTITY_FILE="$EXPLICIT_IDENTITY_FILE"
                FINAL_PUB_KEY=$(cat "$EXPLICIT_IDENTITY_FILE" 2>/dev/null || true)
            elif [ -n "$EXPLICIT_KEY" ]; then
                # 在 SSH Agent 中匹配
                local matched_agent_key=""
                while IFS= read -r line; do
                    if [ -n "$line" ] && [[ "$line" == *"$EXPLICIT_KEY"* ]]; then
                        matched_agent_key="$line"
                        break
                    fi
                done < <(ssh-add -L 2>/dev/null || true)
                
                if [ -n "$matched_agent_key" ]; then
                    echo "$matched_agent_key" > "$AGENT_PUB_FILE"
                    FINAL_IDENTITY_FILE="$AGENT_PUB_FILE"
                    FINAL_PUB_KEY="$matched_agent_key"
                elif [ -f "$HOME/.ssh/${EXPLICIT_KEY}" ]; then
                    FINAL_IDENTITY_FILE="$HOME/.ssh/${EXPLICIT_KEY}"
                elif [ -f "$HOME/.ssh/${EXPLICIT_KEY}.pem" ]; then
                    FINAL_IDENTITY_FILE="$HOME/.ssh/${EXPLICIT_KEY}.pem"
                fi
            fi
            
            if [ -z "$FINAL_IDENTITY_FILE" ]; then
                local agent_keys=()
                while IFS= read -r line; do
                    [ -n "$line" ] && agent_keys+=("$line")
                done < <(ssh-add -L 2>/dev/null || true)
                
                if [ ${#agent_keys[@]} -gt 0 ]; then
                    echo "${agent_keys[0]}" > "$AGENT_PUB_FILE"
                    FINAL_IDENTITY_FILE="$AGENT_PUB_FILE"
                    FINAL_PUB_KEY="${agent_keys[0]}"
                elif [ -f "$HOME/.ssh/id_ed25519" ]; then
                    FINAL_IDENTITY_FILE="$HOME/.ssh/id_ed25519"
                elif [ -f "$HOME/.ssh/id_rsa" ]; then
                    FINAL_IDENTITY_FILE="$HOME/.ssh/id_rsa"
                fi
            fi
        else
            local AUTH_MAIN_CHOICE="1"
            select_menu "🔑 请选择初始认证方式 (使用 ↑/↓ 方向键切换，按 Enter 确认):" \
                "密钥登录 / SSH Agent (使用已有私钥、Agent 托管密钥或现场新建密钥)" \
                "密码登录 (通过初始密码连接并自动向服务器注入本节点公钥)"
            AUTH_MAIN_CHOICE="$MENU_CHOICE"

            if [ "$AUTH_MAIN_CHOICE" = "1" ]; then
                local KEY_SRC_CHOICE="1"
                select_menu "🔐 请选择您的密钥来源 (使用 ↑/↓ 方向键切换，按 Enter 确认):" \
                    "SSH Agent 托管密钥 (Bitwarden / 1Password / 系统 Agent)" \
                    "本地已有私钥文件 (如 ~/.ssh/id_ed25519、~/.ssh/id_rsa 或云厂商 .pem)" \
                    "为此节点现场生成全新专属密钥对 (自动生成 Ed25519 密钥对)"
                KEY_SRC_CHOICE="$MENU_CHOICE"

                case "$KEY_SRC_CHOICE" in
                    1)
                        mkdir -p "$HOME/.ssh/pub"
                        local AGENT_PUB_FILE="$HOME/.ssh/pub/${SSH_ALIAS}.pub"
                        local agent_keys=()
                        while IFS= read -r line; do
                            [ -n "$line" ] && agent_keys+=("$line")
                        done < <(ssh-add -L 2>/dev/null || true)

                        if [ ${#agent_keys[@]} -eq 0 ]; then
                            echo "   ❌ SSH Agent 中没有任何密钥！请先执行 ssh-add <私钥>。"
                            exit 1
                        elif [ ${#agent_keys[@]} -eq 1 ]; then
                            echo "   🎯 SSH Agent 中仅有一把密钥，自动选中: ${agent_keys[0]##* }"
                            echo "${agent_keys[0]}" > "$AGENT_PUB_FILE"
                        else
                            local options=()
                            for k in "${agent_keys[@]}"; do
                                local comment=$(echo "$k" | awk '{print $3}')
                                options+=("$comment")
                            done
                            select_menu "📋 检测到多个 Agent 密钥，请选择 (↑/↓ 切换，Enter 确认):" "${options[@]}"
                            local sel_idx=$((MENU_CHOICE - 1))
                            echo "   🎯 您选择了: ${options[$sel_idx]}"
                            echo "${agent_keys[$sel_idx]}" > "$AGENT_PUB_FILE"
                        fi

                        FINAL_IDENTITY_FILE="$AGENT_PUB_FILE"
                        FINAL_PUB_KEY=$(cat "$AGENT_PUB_FILE" 2>/dev/null || true)
                        ;;
                    2)
                        local DEFAULT_LOCAL_KEY="$HOME/.ssh/id_ed25519"
                        [ ! -f "$DEFAULT_LOCAL_KEY" ] && DEFAULT_LOCAL_KEY="$HOME/.ssh/id_rsa"
                        
                        read -p "📁 请输入本地私钥绝对路径 (直接回车默认 $DEFAULT_LOCAL_KEY): " INPUT_PEM
                        local LOCAL_KEY_PATH="${INPUT_PEM:-$DEFAULT_LOCAL_KEY}"
                        LOCAL_KEY_PATH="${LOCAL_KEY_PATH/#\~/$HOME}"

                        if [ ! -f "$LOCAL_KEY_PATH" ]; then
                            echo "   ❌ 严重错误：指定的私钥文件不存在: $LOCAL_KEY_PATH"
                            exit 1
                        fi
                        chmod 600 "$LOCAL_KEY_PATH" 2>/dev/null || true
                        FINAL_IDENTITY_FILE="$LOCAL_KEY_PATH"
                        
                        mkdir -p "$HOME/.ssh/pub"
                        local EXTRACTED_PUB=$(ssh-keygen -y -f "$LOCAL_KEY_PATH" 2>/dev/null || true)
                        if [ -n "$EXTRACTED_PUB" ]; then
                            echo "$EXTRACTED_PUB" > "$HOME/.ssh/pub/${SSH_ALIAS}.pub"
                            FINAL_PUB_KEY="$EXTRACTED_PUB"
                        fi
                        ;;
                    3)
                        mkdir -p "$HOME/.ssh/pub"
                        local NEW_KEY_PATH="$HOME/.ssh/${SSH_ALIAS}"
                        local NEW_PUB_PATH="$HOME/.ssh/pub/${SSH_ALIAS}.pub"
                        
                        echo "   ⚙️ 正在为节点 [$SSH_ALIAS] 生成高强度 Ed25519 专属密钥对..."
                        ssh-keygen -t ed25519 -N "" -C "$SSH_ALIAS" -f "$NEW_KEY_PATH" >/dev/null 2>&1
                        cp "${NEW_KEY_PATH}.pub" "$NEW_PUB_PATH"
                        chmod 600 "$NEW_KEY_PATH"
                        
                        FINAL_IDENTITY_FILE="$NEW_KEY_PATH"
                        FINAL_PUB_KEY=$(cat "$NEW_PUB_PATH")
                        echo "   ✅ 专属密钥已生成并保存在: $NEW_KEY_PATH"
                        
                        echo "   ⚠️ 新实例尚未包含此新密钥。我们将使用您现有的临时密钥将其安全注入。"
                        read -p "📁 请输入现有私钥路径 (直接回车尝试使用 SSH Agent 自动注入): " TEMP_PEM
                        if [ -n "$TEMP_PEM" ]; then
                            TEMP_PEM="${TEMP_PEM/#\~/$HOME}"
                            ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i "$TEMP_PEM" "$DETECTED_USER@$TARGET_IP" "echo \"$FINAL_PUB_KEY\" >> ~/.ssh/authorized_keys" >/dev/null 2>&1 || true
                        else
                            ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$DETECTED_USER@$TARGET_IP" "echo \"$FINAL_PUB_KEY\" >> ~/.ssh/authorized_keys" >/dev/null 2>&1 || true
                        fi
                        echo "   ✅ 尝试自动注入专属公钥完成！"
                        ;;
                esac

                # 打印公钥并提示远端放行
                if [ -n "$FINAL_PUB_KEY" ]; then
                    echo ""
                    echo "📋 节点 [$SSH_ALIAS] 对应的公钥如下:"
                    echo "   $FINAL_PUB_KEY"
                    echo ""
                    echo "💡 提示：如果远端云服务器（如 AWS Lightsail / EC2）尚未放行此公钥，请在网页终端执行:"
                    echo "   echo \"$FINAL_PUB_KEY\" >> ~/.ssh/authorized_keys"
                    echo ""
                fi

            elif [ "$AUTH_MAIN_CHOICE" = "2" ]; then
                mkdir -p "$HOME/.ssh/pub"
                local PUB_KEY_PATH="$HOME/.ssh/pub/${SSH_ALIAS}.pub"
                if [ ! -f "$PUB_KEY_PATH" ]; then
                    local AGENT_KEY=$(ssh-add -L 2>/dev/null | grep -i "$SSH_ALIAS" || true)
                    if [ -n "$AGENT_KEY" ]; then
                        echo "$AGENT_KEY" > "$PUB_KEY_PATH"
                    elif [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
                        cp "$HOME/.ssh/id_ed25519.pub" "$PUB_KEY_PATH"
                    elif [ -f "$HOME/.ssh/id_rsa.pub" ]; then
                        cp "$HOME/.ssh/id_rsa.pub" "$PUB_KEY_PATH"
                    fi
                fi

                echo "   🔑 即将通过密码连接并注入公钥，请在提示时输入服务器登录密码..."
                if [ "$DEBUG_MODE" = "true" ]; then
                    ssh-copy-id -f -i "$PUB_KEY_PATH" -o StrictHostKeyChecking=no -o PubkeyAuthentication=no "$DETECTED_USER@$TARGET_IP"
                else
                    ssh-copy-id -f -i "$PUB_KEY_PATH" -o StrictHostKeyChecking=no -o PubkeyAuthentication=no "$DETECTED_USER@$TARGET_IP" >/dev/null 2>&1
                fi
                FINAL_IDENTITY_FILE="$PUB_KEY_PATH"
            fi
        fi

        echo "   -> 正在使用身份 [$DETECTED_USER] 进行免密握手探活..."
        local IDENTITY_OPT=()
        if [ -n "$FINAL_IDENTITY_FILE" ] && [ -f "$FINAL_IDENTITY_FILE" ]; then
            IDENTITY_OPT=(-i "$FINAL_IDENTITY_FILE" -o IdentitiesOnly=yes)
        fi

        local alive="false"
        for try_i in {1..8}; do
            if ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no "${IDENTITY_OPT[@]}" "$DETECTED_USER@$TARGET_IP" "echo 'alive'" >/dev/null 2>&1; then
                alive="true"
                break
            fi
            sleep 3
        done

        if [ "$alive" = "true" ]; then
            echo "   ✅ 探活成功！已通过身份 [$DETECTED_USER] 及对应密钥建立免密连接。"
        else
            echo "   ❌ 免密探测失败：目标服务器拒绝了该密钥认证。"
            echo "   💡 建议排查："
            echo "      1. 初始用户名是否正确 (当前输入: $DETECTED_USER，Debian 为 admin, Ubuntu 为 ubuntu)"
            echo "      2. 远端 ~/.ssh/authorized_keys 中是否已包含此公钥"
            echo "      3. 云厂商安全组 / 防火墙 22 端口是否放行"
            exit 1
        fi

        local CONFIG_IDENTITY_VAL=""
        if [ -n "$FINAL_IDENTITY_FILE" ]; then
            CONFIG_IDENTITY_VAL=$(echo "$FINAL_IDENTITY_FILE" | sed "s|^$HOME|~|")
        elif [ -f "$HOME/.ssh/pub/${SSH_ALIAS}.pub" ]; then
            CONFIG_IDENTITY_VAL="~/.ssh/pub/${SSH_ALIAS}.pub"
        fi

        local IDENTITY_LINE=""
        if [ -n "$CONFIG_IDENTITY_VAL" ]; then
            local IDENTITY_ONLY_LINE="    IdentitiesOnly yes"
            if grep -i -q "^[[:space:]]*IdentitiesOnly" ~/.ssh/config 2>/dev/null; then
                IDENTITY_ONLY_LINE=""
            fi
            if [ -n "$IDENTITY_ONLY_LINE" ]; then
                IDENTITY_LINE="    IdentityFile $CONFIG_IDENTITY_VAL
$IDENTITY_ONLY_LINE"
            else
                IDENTITY_LINE="    IdentityFile $CONFIG_IDENTITY_VAL"
            fi
        fi

        if ! grep -q -E "^Host $SSH_ALIAS$" ~/.ssh/config 2>/dev/null; then
            local STRICT_KEY_LINE="    StrictHostKeyChecking accept-new"
            # 检测全局是否已经配置了 StrictHostKeyChecking
            if grep -i -q "^[[:space:]]*StrictHostKeyChecking" ~/.ssh/config 2>/dev/null; then
                STRICT_KEY_LINE=""
            fi

            cat <<CFG >> ~/.ssh/config

Host $SSH_ALIAS
    HostName $TARGET_IP
    User $DETECTED_USER
$IDENTITY_LINE
$STRICT_KEY_LINE
CFG
            echo "✅ ~/.ssh/config 写入初始配置完成！"
        else
            # 为了保证绝对的幂等性，如果以前初始化被中断或重装了机器，强制将 HostName 恢复为纯 IP，并移除被篡改的高危端口和影子用户，恢复出厂设置。
            export PY_CONFIG_PATH="$HOME/.ssh/config"
            export PY_ALIAS="$SSH_ALIAS"
            export PY_IP="$TARGET_IP"
            export PY_USER="$DETECTED_USER"
            export PY_IDENTITY_LINE="$CONFIG_IDENTITY_VAL"
            uv run python3 -c "
import os, re
config_path = os.getenv('PY_CONFIG_PATH', '')
alias = os.getenv('PY_ALIAS', '')
ip = os.getenv('PY_IP', '')
user = os.getenv('PY_USER', '')
identity_line = os.getenv('PY_IDENTITY_LINE', '')
try:
    with open(config_path, 'r') as f:
        content = f.read()
    
    # 提取别名对应的块
    block_pattern = r'(Host\s+' + re.escape(alias) + r'\b(.*?))(?=\nHost\s|\Z)'
    match = re.search(block_pattern, content, flags=re.DOTALL)
    if match:
        block = match.group(1)
        # 替换 HostName
        block = re.sub(r'(^[ \t]*HostName\s+)(\S+)', r'\g<1>' + ip, block, flags=re.MULTILINE)
        # 替换 User
        block = re.sub(r'(^[ \t]*User\s+)(\S+)', r'\g<1>' + user, block, flags=re.MULTILINE)
        # 删除 Port
        block = re.sub(r'^[ \t]*Port\s+\d+\n?', '', block, flags=re.MULTILINE)
        if identity_line:
            if re.search(r'^[ \t]*IdentityFile\s+', block, flags=re.MULTILINE):
                block = re.sub(r'(^[ \t]*IdentityFile\s+)(\S+)', r'\g<1>' + identity_line, block, flags=re.MULTILINE)
            else:
                block = block.rstrip() + f'\n    IdentityFile {identity_line}\n'
            if not re.search(r'^[ \t]*IdentitiesOnly\s+', content, flags=re.MULTILINE | re.IGNORECASE):
                if not re.search(r'^[ \t]*IdentitiesOnly\s+', block, flags=re.MULTILINE | re.IGNORECASE):
                    block = block.rstrip() + '\n    IdentitiesOnly yes\n'
        
        new_content = content[:match.start()] + block + content[match.end():]
        with open(config_path, 'w') as f:
            f.write(new_content)
except Exception as e:
    pass
" 2>/dev/null || true
        fi
    fi

    local REAL_HOST=$(get_real_host)
    echo "🔍 正在读取 SSH 配置中的真实域名/IP: $REAL_HOST"

    echo "🔍 正在探测目标机器 IPv4 地址..."
    local IPV4=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -4 -s --connect-timeout 5 ifconfig.me" || true)
    if [ -z "$IPV4" ]; then
        echo "❌ 严重错误：免密安全接管失败！请检查公钥状态。"
        exit 1
    fi
    IFS=. read -r a b c d <<< "$IPV4"
    local C_BASE=$(( (c / 4) * 4 ))
    local IPV4_CIDR="${a}.${b}.${C_BASE}.0/22"
    echo "✅ 探测到 IPv4: $IPV4, 推算网段为: $IPV4_CIDR"

    echo "🔍 正在探测目标机器 IPv6 地址..."
    local IPV6=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -6 -s --connect-timeout 5 ifconfig.me || echo 'none'" || true)
    local IPV6_CIDR="none"
    if [ "$IPV6" != "none" ] && [ -n "$IPV6" ]; then
        IPV6_CIDR=$(uv run python3 -c "import sys, ipaddress; print(str(ipaddress.IPv6Network(f'{sys.argv[1]}/112', strict=False)))" "$IPV6" 2>/dev/null)
        if [ -z "$IPV6_CIDR" ]; then
            local V6_PREFIX=$(echo "$IPV6" | awk -F':' '{print $1":"$2":"$3":"$4}')
            IPV6_CIDR="${V6_PREFIX}::/64"
        fi
        echo "✅ 探测到 IPv6: $IPV6, 推算网段为: $IPV6_CIDR"
    fi

    # 生成域名并获取返回的新域名
    local FULL_DOMAIN=$(generate_cf_domain "$IPV4" "$IPV6" "$SSH_ALIAS" | tail -n 1)
    REAL_HOST=$FULL_DOMAIN

    echo "☁️ 正在触发 GitHub Actions 云端扫描..."
    local SCAN_TRIGGERED=false
    if [ -n "$GATEWAY_URL" ]; then
        local payload
        if command -v jq >/dev/null 2>&1; then
            payload=$(jq -n \
                --arg ip "$IPV4" \
                --arg v4 "$IPV4_CIDR" \
                --arg v6 "$IPV6_CIDR" \
                --arg alias "$SSH_ALIAS" \
                '{target_ip: $ip, target_cidr_v4: $v4, target_cidr_v6: $v6, alias: $alias}')
        else
            payload="{\"target_ip\":\"$IPV4\",\"target_cidr_v4\":\"$IPV4_CIDR\",\"target_cidr_v6\":\"$IPV6_CIDR\",\"alias\":\"$SSH_ALIAS\"}"
        fi
        local auth_header=()
        if [ -n "$GATEWAY_AUTH_KEY" ]; then
            auth_header=(-H "Authorization: Bearer $GATEWAY_AUTH_KEY")
        fi
        if curl -s -f -X POST "${auth_header[@]}" \
            -H "Content-Type: application/json" \
            "$GATEWAY_URL/api/gh-dispatch" \
            -d "$payload" >/dev/null 2>&1; then
            SCAN_TRIGGERED=true
        fi
    fi

    if [ "$SCAN_TRIGGERED" = false ] && [ -n "$GH_TOKEN" ]; then
        local payload
        if command -v jq >/dev/null 2>&1; then
            payload=$(jq -n \
                --arg ip "$IPV4" \
                --arg v4 "$IPV4_CIDR" \
                --arg v6 "$IPV6_CIDR" \
                --arg alias "$SSH_ALIAS" \
                '{event_type: "scan_trigger", client_payload: {target_ip: $ip, target_cidr_v4: $v4, target_cidr_v6: $v6, alias: $alias}}')
        else
            payload="{\"event_type\":\"scan_trigger\",\"client_payload\":{\"target_ip\":\"$IPV4\",\"target_cidr_v4\":\"$IPV4_CIDR\",\"target_cidr_v6\":\"$IPV6_CIDR\",\"alias\":\"$SSH_ALIAS\"}}"
        fi
        if curl -s -f -X POST \
            -H "Accept: application/vnd.github.v3+json" \
            -H "Authorization: Bearer $GH_TOKEN" \
            https://api.github.com/repos/svbmwjwj/snack-connoisseur/dispatches \
            -d "$payload" >/dev/null 2>&1; then
            SCAN_TRIGGERED=true
        fi
    fi

    if [ "$SCAN_TRIGGERED" = false ] && command -v gh >/dev/null 2>&1; then
        if gh api repos/svbmwjwj/snack-connoisseur/dispatches -f event_type='scan_trigger' -F "client_payload[target_ip]=$IPV4" -F "client_payload[target_cidr_v4]=$IPV4_CIDR" -F "client_payload[target_cidr_v6]=$IPV6_CIDR" -F "client_payload[alias]=$SSH_ALIAS" >/dev/null 2>&1; then
            SCAN_TRIGGERED=true
        fi
    fi

    if [ "$SCAN_TRIGGERED" = true ]; then
        echo "✅ 云端扫描已成功触发！"
    else
        echo "⚠️ 无法通过 Gateway / GH_TOKEN / gh 触发云端扫描，请检查 .env 中的网关配置或手动在 GitHub 触发。"
    fi

    # 对于可能没有 admin 用户的主机，先确认 DOCKER_APP_DIR 所在主目录
    local REMOTE_HOME=$(ssh "$SSH_ALIAS" "eval echo ~\$USER")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"
    
    local SCP_OPT=()
    if [ "$DEBUG_MODE" = "false" ]; then SCP_OPT=("-q"); fi

    echo "🛡️ 正在装配并同步节点基础组件 (runner/自愈/体检/脱敏凭据)..."
    sync_node_scripts "$SSH_ALIAS" "$IPV4" "$FULL_DOMAIN"

    ssh "$SSH_ALIAS" "[ -f ${DOCKER_APP_DIR}/compose.yml ]" || scp "${SCP_OPT[@]}" templates/compose.yml.template "$SSH_ALIAS":${DOCKER_APP_DIR}/compose.yml
    ssh "$SSH_ALIAS" "[ -f ${DOCKER_APP_DIR}/conf/config.json ]" || scp "${SCP_OPT[@]}" templates/config.json.template "$SSH_ALIAS":${DOCKER_APP_DIR}/conf/config.json

    local SHADOW_USER=""
    local NEW_SSH_PORT="22"

    if [ "$SECURE_MODE" = "true" ]; then
        # 1. 影子管理员词库 (科幻名)
        local SCIFI_NAMES=("arrakis" "replicant" "skynet" "gotham" "trantor" "jedi" "cyberdyne" "matrix" "nostromo" "tesseract")
        SHADOW_USER=${SCIFI_NAMES[$RANDOM % ${#SCIFI_NAMES[@]}]}
        NEW_SSH_PORT=$((RANDOM % 50000 + 10000))
        
        module_harden_system "$SSH_ALIAS" "$TARGET_IP" "$NEW_SSH_PORT" "$SHADOW_USER"
        local harden_status=$?
        if [ $harden_status -ne 0 ]; then
            return $harden_status
        fi

        DOCKER_APP_DIR="/home/$SHADOW_USER/docker-apps/xray"
    else
        echo "⚙️ 正在安装基础依赖及 Docker..."
        ssh "$SSH_ALIAS" "
          set -e
          sudo apt-get update -y >/dev/null 2>&1 && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y jq cron curl iputils-ping dnsutils >/dev/null 2>&1
        "
    fi

    # 统一安装启动 Docker 与 内核优化
    echo "🐳 启动 X-ray 服务与定时自愈探针..."
    ssh "$SSH_ALIAS" "
      set -e
      if ! command -v docker >/dev/null 2>&1; then
          curl -fsSL https://get.docker.com | sudo sh >/dev/null 2>&1
      fi
      
      # 内核网络优化 (BBR + TCP Fast Open 3)
      sudo sh -c 'cat <<EOF > /etc/sysctl.d/99-reality.conf
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.tcp_fastopen=3
EOF'
      sudo sysctl --system >/dev/null 2>&1 || true

      cd ${DOCKER_APP_DIR}
      sudo docker compose up -d >/dev/null 2>&1
      if ! command -v crontab >/dev/null 2>&1; then if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -y && sudo apt-get install -y cron; fi; fi
      (sudo -u \$(whoami) crontab -l 2>/dev/null | grep -v -E 'reality_rotate.sh|runner.sh'; echo '*/15 * * * * ${DOCKER_APP_DIR}/runner.sh > ${DOCKER_APP_DIR}/rotate.log 2>&1') | sudo -u \$(whoami) crontab -
    "
    
    echo "🚀 正在部署异步守护进程，接管后续的配置与测试..."
    cp templates/async_deploy.template.sh async_deploy.sh
    sed -i '' -e "s|PLACEHOLDER_IP|$IPV4|g" -e "s|PLACEHOLDER_ALIAS|$SSH_ALIAS|g" -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" async_deploy.sh 2>/dev/null || \
    sed -i -e "s|PLACEHOLDER_IP|$IPV4|g" -e "s|PLACEHOLDER_ALIAS|$SSH_ALIAS|g" -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" async_deploy.sh
    
    scp "${SCP_OPT[@]}" async_deploy.sh "$SSH_ALIAS":${DOCKER_APP_DIR}/async_deploy.sh
    rm -f async_deploy.sh
    
    echo "🎉 [$SSH_ALIAS] 阶段一：服务器基础环境搭建完成，已触发云端 IP 扫描。"
    
    if [ "$DEBUG_MODE" = "true" ]; then
        echo "🐛 [Debug] 调试模式已开启！正在连接终端查看后台扫描与配置全景日志..."
        ssh -t "$SSH_ALIAS" "chmod +x ${DOCKER_APP_DIR}/async_deploy.sh && cd ${DOCKER_APP_DIR} && ./async_deploy.sh --debug"
        echo "✅ [Debug] 调试执行完毕。"
    else
        ssh "$SSH_ALIAS" "chmod +x ${DOCKER_APP_DIR}/async_deploy.sh && cd ${DOCKER_APP_DIR} && nohup ./async_deploy.sh > async_deploy.log 2>&1 < /dev/null &"
        echo "💡 [提示] 您现在可以安全关闭本窗口。后续的部署与体检流程将由服务器在后台全自动接管，进度会实时推送到 Telegram。"
    fi

    # 全部部署操作完成后，验证 DNS 状态并尝试升级 ~/.ssh/config 中的 HostName 为域名
    upgrade_ssh_config_hostname "$SSH_ALIAS" "$FULL_DOMAIN"
}
