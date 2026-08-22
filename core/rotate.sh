#!/bin/bash
# Snack Connoisseur - Fingerprint, DNS & IP Rotation Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$LIB_DIR/ssh.sh" ]; then
    source "$LIB_DIR/ssh.sh"
fi
if [ -f "$SCRIPT_DIR/update.sh" ]; then
    source "$SCRIPT_DIR/update.sh"
fi

SSH_CONFIG_PATH="${TEST_SSH_CONFIG:-$HOME/.ssh/config}"

function set_env_var() {
    local key="$1"
    local val="$2"
    local env_file="$REPO_DIR/.env"
    if [ ! -f "$env_file" ]; then
        touch "$env_file"
    fi
    if grep -q "^${key}=" "$env_file" 2>/dev/null; then
        sed -i '' -e "s|^${key}=.*|${key}=\"${val}\"|g" "$env_file" 2>/dev/null || \
        sed -i -e "s|^${key}=.*|${key}=\"${val}\"|g" "$env_file"
    else
        echo "${key}=\"${val}\"" >> "$env_file"
    fi
}

function check_cf_credentials() {
    if [ -z "$CF_API_TOKEN" ]; then
        if [ -t 0 ]; then
            read -p "⚠️ 请输入您的 Cloudflare API Token: " CF_API_TOKEN
            set_env_var "CF_API_TOKEN" "$CF_API_TOKEN"
        else
            if [ "$CNSR_LANG" = "en" ]; then
                echo "❌ Missing CF_API_TOKEN environment variable!"
            else
                echo "❌ 缺少 CF_API_TOKEN 环境变量！"
            fi
            return 1
        fi
    fi
    if [ -z "$CF_ZONE_ID" ]; then
        if [ -t 0 ]; then
            read -p "⚠️ 请输入您的 Cloudflare Zone ID: " CF_ZONE_ID
            set_env_var "CF_ZONE_ID" "$CF_ZONE_ID"
        else
            if [ "$CNSR_LANG" = "en" ]; then
                echo "❌ Missing CF_ZONE_ID environment variable!"
            else
                echo "❌ 缺少 CF_ZONE_ID 环境变量！"
            fi
            return 1
        fi
    fi
}

function generate_cf_domain() {
    local ip_v4="$1"
    local ip_v6="$2"
    local alias="$3"
    
    check_cf_credentials || return 1
    if [ "$CNSR_LANG" = "en" ]; then
        echo "🌐 Generating enterprise-style camouflage domain..." >&2
    else
        echo "🌐 正在生成大厂风格伪装域名..." >&2
    fi
    local PREFIX_LIST=("api-gateway" "auth-svc" "user-metrics" "static-cdn" "edge-cache" "log-stream")
    local SUFFIX=$((RANDOM % 900 + 100))
    local CF_SUBDOMAIN="${PREFIX_LIST[$RANDOM % ${#PREFIX_LIST[@]}]}-v${SUFFIX}"

    local CF_BASE="${CF_BASE_DOMAIN:-}"
    if [ -z "$CF_BASE" ]; then
        for try_cf in 1 2 3; do
            local CF_DOMAIN_RESPONSE=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID" \
                -H "Authorization: Bearer $CF_API_TOKEN" \
                -H "Content-Type: application/json")
            CF_BASE=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print(data.get('result', {}).get('name', ''))
except Exception:
    pass
" <<< "$CF_DOMAIN_RESPONSE" 2>/dev/null)
            if [ -n "$CF_BASE" ] && [ "$CF_BASE" != "null" ]; then
                export CF_BASE_DOMAIN="$CF_BASE"
                break
            fi
            sleep 1
        done
    fi

    if [ "$CF_BASE" = "null" ] || [ -z "$CF_BASE" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Failed to fetch Cloudflare domain! Check Token or Zone ID." >&2
        else
            echo "❌ 无法获取 Cloudflare 域名，请检查 Token 或 Zone ID！" >&2
        fi
        return 1
    fi

    local FULL_DOMAIN="${CF_SUBDOMAIN}.${CF_BASE}"
    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Generated camouflage domain: $FULL_DOMAIN" >&2
        echo "🧹 Cleaning legacy redundant DNS records (matching IP or alias $alias)..." >&2
    else
        echo "✅ 生成伪装域名: $FULL_DOMAIN" >&2
        echo "🧹 正在清理当前服务器历史冗余解析记录 (匹配 IP 或别名 $alias)..." >&2
    fi

    local OLD_RECS_JSON=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records?per_page=100" \
        -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json")

    local OLD_A_IDS=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    ip = '$ip_v4'
    al = '$alias'
    ids = [r['id'] for r in data.get('result', []) if r.get('type') == 'A' and (r.get('content') == ip or r.get('comment') == al)]
    print(' '.join(ids))
except Exception:
    pass
" <<< "$OLD_RECS_JSON" 2>/dev/null)

    for id in $OLD_A_IDS; do
        curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records/$id" \
            -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" > /dev/null
    done

    if [ "$CNSR_LANG" = "en" ]; then
        echo "☁️ Creating latest A record via Cloudflare API..." >&2
    else
        echo "☁️ 正在通过 Cloudflare API 创建最新 A 记录..." >&2
    fi
    local A_RESP=$(curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H "Content-Type: application/json" \
        --data '{"type":"A","name":"'"$CF_SUBDOMAIN"'","content":"'"$ip_v4"'","ttl":1,"proxied":false,"comment":"'"$alias"'"}' 2>/dev/null)

    local A_SUCCESS=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print('true' if data.get('success') else 'false')
except Exception:
    print('false')
" <<< "$A_RESP" 2>/dev/null)

    if [ "$A_SUCCESS" != "true" ]; then
        local ERR_MSG=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    errors = data.get('errors', [])
    print(errors[0].get('message', 'Unknown Cloudflare API error') if errors else 'Unknown Cloudflare API error')
except Exception:
    print('Unknown Cloudflare API error')
" <<< "$A_RESP" 2>/dev/null)
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Cloudflare DNS A record creation failed: $ERR_MSG" >&2
        else
            echo "❌ Cloudflare DNS A 记录创建失败: $ERR_MSG" >&2
        fi
        return 1
    fi
    
    if [ "$ip_v6" != "none" ] && [ -n "$ip_v6" ]; then
        local OLD_AAAA_IDS=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    ip = '$ip_v6'
    al = '$alias'
    ids = [r['id'] for r in data.get('result', []) if r.get('type') == 'AAAA' and (r.get('content') == ip or r.get('comment') == al)]
    print(' '.join(ids))
except Exception:
    pass
" <<< "$OLD_RECS_JSON" 2>/dev/null)

        for id in $OLD_AAAA_IDS; do
            curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records/$id" \
                -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" > /dev/null
        done

        if [ "$CNSR_LANG" = "en" ]; then
            echo "☁️ Creating latest AAAA record via Cloudflare API..." >&2
        else
            echo "☁️ 正在通过 Cloudflare API 创建最新 AAAA 记录..." >&2
        fi
        local AAAA_RESP=$(curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \
            -H "Authorization: Bearer $CF_API_TOKEN" \
            -H "Content-Type: application/json" \
            --data '{"type":"AAAA","name":"'"$CF_SUBDOMAIN"'","content":"'"$ip_v6"'","ttl":1,"proxied":false,"comment":"'"$alias"'"}' 2>/dev/null)

        local AAAA_SUCCESS=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print('true' if data.get('success') else 'false')
except Exception:
    print('false')
" <<< "$AAAA_RESP" 2>/dev/null)

        if [ "$AAAA_SUCCESS" != "true" ]; then
            local ERR_V6=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    errors = data.get('errors', [])
    print(errors[0].get('message', 'Unknown Cloudflare API error') if errors else 'Unknown Cloudflare API error')
except Exception:
    print('Unknown Cloudflare API error')
" <<< "$AAAA_RESP" 2>/dev/null)
            if [ "$CNSR_LANG" = "en" ]; then
                echo "❌ Cloudflare DNS AAAA record creation failed: $ERR_V6" >&2
            else
                echo "❌ Cloudflare DNS AAAA 记录创建失败: $ERR_V6" >&2
            fi
            return 1
        fi
    fi
    echo "$FULL_DOMAIN"
}

function module_rotate_sni() {
    local alias="${1:-$SSH_ALIAS}"
    local is_dry="${DRY_RUN:-false}"

    for arg in "$@"; do
        if [ "$arg" = "--dry-run" ] || [ "$arg" = "-dry-run" ]; then
            is_dry="true"
        fi
    done

    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh rotate-sni <alias> [--dry-run])"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh rotate-sni <别名> [--dry-run])"
        fi
        return 1
    fi

    if [ "$is_dry" = "true" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "🧪 Simulating Dry-Run SNI domain evaluation for [$alias]..."
        else
            echo "🧪 模拟 Dry-Run 纯选域演练: [$alias]..."
        fi
    else
        if [ "$CNSR_LANG" = "en" ]; then
            echo "🔄 Forcing X-ray fingerprint rotation for [$alias]..."
        else
            echo "🔄 强制触发 X-ray 指纹轮换: [$alias]..."
        fi
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

    local cmd_prefix=""
    if [ "$is_dry" = "true" ]; then
        cmd_prefix="IS_TEST_MODE=1 DRY_RUN=1"
    fi

    ssh "${ssh_opts[@]}" "$alias" "
        set -e
        if [ ! -f ${DOCKER_APP_DIR}/runner.sh ] && [ ! -f ${DOCKER_APP_DIR}/reality_rotate.sh ]; then
            echo '❌ 未找到轮换脚本！'
            exit 1
        fi
        cd ${DOCKER_APP_DIR}
        if [ -f ./reality_rotate.sh ]; then
            $cmd_prefix bash ./reality_rotate.sh --force
        elif [ -f ./runner.sh ]; then
            $cmd_prefix bash ./runner.sh --force
        fi
    "
    local rot_status=$?
    if [ $rot_status -ne 0 ]; then
        return $rot_status
    fi

    if [ "$is_dry" != "true" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "✅ Fingerprint rotation completed! New VLESS node config dispatched to Telegram."
        else
            echo "✅ 指纹轮换完毕，新的 VLESS 节点信息已推送到 Telegram！"
        fi
    fi
    return 0
}

# Alias for backwards compatibility
function module_rotate_xray() {
    module_rotate_sni "$@"
}

function module_rotate_dns() {
    local alias="${1:-$SSH_ALIAS}"
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh rotate-dns <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh rotate-dns <别名>)"
        fi
        return 1
    fi

    check_cf_credentials || return 1
    if [ "$CNSR_LANG" = "en" ]; then
        echo "🌐 Rotating DNS domain matrix for [$alias]..."
    else
        echo "🌐 开始洗白 DNS 域名矩阵: [$alias]..."
    fi

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    local IPV4=$(ssh "${ssh_opts[@]}" "$alias" "curl -4 -s --connect-timeout 5 ifconfig.me || curl -4 -s --connect-timeout 5 api.ipify.org" 2>/dev/null || true)
    local IPV6=""
    if declare -f detect_remote_ipv6 >/dev/null 2>&1; then
        IPV6=$(detect_remote_ipv6 "$alias")
    fi
    [ -z "$IPV6" ] && IPV6="none"

    if [ -z "$IPV4" ]; then
        if declare -f get_real_host >/dev/null 2>&1; then
            IPV4=$(get_real_host "$alias")
        fi
    fi

    local FULL_DOMAIN
    FULL_DOMAIN=$(generate_cf_domain "$IPV4" "$IPV6" "$alias")
    local gen_status=$?
    if [ $gen_status -ne 0 ] || [ -z "$FULL_DOMAIN" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Failed to generate or create Cloudflare domain."
        else
            echo "❌ 生成或同步 Cloudflare 域名失败。"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🔧 Updating server configuration with domain $FULL_DOMAIN..."
    else
        echo "🔧 正在将新域名 $FULL_DOMAIN 下发至服务器配置中..."
    fi

    local REMOTE_HOME=$(ssh "${ssh_opts[@]}" "$alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    ssh "${ssh_opts[@]}" "$alias" "
        set -e
        sed -i 's/^SERVER_HOST=.*/SERVER_HOST=\"$FULL_DOMAIN\"/' ${DOCKER_APP_DIR}/reality_rotate.sh 2>/dev/null || true
        sed -i 's/^SERVER_HOST=.*/SERVER_HOST=\"$FULL_DOMAIN\"/' ${DOCKER_APP_DIR}/reality_check.sh 2>/dev/null || true
        sed -i 's/^SERVER_HOST=.*/SERVER_HOST=\"$FULL_DOMAIN\"/' ${DOCKER_APP_DIR}/runner.sh 2>/dev/null || true
    "

    if declare -f upgrade_ssh_config_hostname >/dev/null 2>&1; then
        upgrade_ssh_config_hostname "$alias" "$FULL_DOMAIN"
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Camouflage domain updated. Next cron run will use the new domain. Run './cnsr.sh rotate-sni $alias' to apply immediately."
    else
        echo "✅ 伪装域名已更新，下一次 cron 将自动使用新域名！您也可以执行 ./cnsr.sh rotate-sni $alias 强制立刻生效。"
    fi
    return 0
}

function module_rotate_ip() {
    local alias="${1:-$SSH_ALIAS}"
    if [ -z "$alias" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh rotate-ip <alias>)"
        else
            echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh rotate-ip <别名>)"
        fi
        return 1
    fi

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    # 4.1 Stateless Probe: check AWS metadata endpoint, fallback to alias prefix inference when IP is blocked
    local AWS_REGION=$(ssh "${ssh_opts[@]}" "$alias" "curl -s -m 2 http://169.254.169.254/latest/meta-data/placement/region" 2>/dev/null || true)
    if [ -z "$AWS_REGION" ]; then
        case "$alias" in
            jp_*|jp-*) AWS_REGION="ap-northeast-1" ;;
            sg_*|sg-*) AWS_REGION="ap-southeast-1" ;;
            us_*|us-*) AWS_REGION="us-east-1" ;;
            kr_*|kr-*) AWS_REGION="ap-northeast-2" ;;
            de_*|de-*) AWS_REGION="eu-central-1" ;;
            uk_*|uk-*) AWS_REGION="eu-west-2" ;;
        esac
    fi

    if [ -z "$AWS_REGION" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Manual node: cloud automated IP rotation is not supported on non-AWS nodes."
        else
            echo "❌ 纯手动节点，不支持云端自动换绑 IP (无法获取实例所在地域)。"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🚨 Initiating AWS Lightsail IP rotation for [$alias] ($AWS_REGION)..."
    else
        echo "🚨 开始对 AWS Lightsail 节点 [$alias] ($AWS_REGION) 执行断臂求生换 IP..."
    fi

    # Delegate to providers/aws.py using uv run
    local ROTATE_RESULT=$(cd "$REPO_DIR" && uv run python3 providers/aws.py rotate-ip --alias "$alias" --region "$AWS_REGION" 2>/dev/null || true)

    local NEW_IP=$(cd "$REPO_DIR" && uv run python3 -c "
import sys, json
try:
    data = json.loads('''$ROTATE_RESULT''')
    print(data.get('new_ip', ''))
except Exception:
    pass
" 2>/dev/null)

    if [ -z "$NEW_IP" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Failed to rotate AWS static IP."
        else
            echo "❌ AWS Lightsail 静态 IP 换绑失败！"
        fi
        return 1
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "   -> New IP allocated: $NEW_IP"
        echo "   -> Updating SSH config to point to $NEW_IP..."
    else
        echo "   -> 获得新 IP: $NEW_IP"
        echo "   -> 更新本地 SSH Config 以指向新 IP..."
    fi

    # Update SSH Config
    if declare -f ensure_ssh_alias >/dev/null 2>&1; then
        local user=""
        if declare -f get_ssh_config >/dev/null 2>&1; then
            user=$(get_ssh_config "$alias" "User")
        fi
        [ -z "$user" ] && user="admin"
        ensure_ssh_alias "$alias" "$NEW_IP" "$user"
    else
        cd "$REPO_DIR" && uv run python3 -c "
import sys, os, re
config_path = os.path.expanduser('$SSH_CONFIG_PATH')
alias = '$alias'
new_ip = '$NEW_IP'
if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        content = f.read()
    pattern = r'(Host\s+' + re.escape(alias) + r'\s+[\s\S]*?HostName\s+)(\S+)'
    new_content = re.sub(pattern, r'\g<1>' + new_ip, content, count=1)
    with open(config_path, 'w') as f:
        f.write(new_content)
" 2>/dev/null || true
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        echo "   -> Waiting for SSH service to recover on new IP..."
    else
        echo "   -> 等待新 IP 的 SSH 端口恢复响应..."
    fi

    for i in {1..15}; do
        if ssh "${ssh_opts[@]}" -o ConnectTimeout=2 "$alias" "echo up" >/dev/null 2>&1; then
            if [ "$CNSR_LANG" = "en" ]; then
                echo "   ✅ SSH connection restored!"
            else
                echo "   ✅ SSH 已恢复连接！"
            fi
            break
        fi
        sleep 2
    done

    # Cascade Cloudflare DNS and X-ray fingerprint rotation
    if [ "$CNSR_LANG" = "en" ]; then
        echo "   -> Cascading Cloudflare DNS update..."
    else
        echo "   -> 级联触发 Cloudflare DNS 更新与服务端节点配置同步..."
    fi
    module_rotate_dns "$alias"

    if [ "$CNSR_LANG" = "en" ]; then
        echo "   -> Triggering remote X-ray fingerprint rotation..."
    else
        echo "   -> 自动触发远端 X-ray 指纹轮换以推送最新节点配置到 Telegram..."
    fi
    module_rotate_sni "$alias"

    if [ "$CNSR_LANG" = "en" ]; then
        echo "🎉 Automated IP rotation and self-healing complete for [$alias]!"
    else
        echo "🎉 自动换 IP 与自愈修复全部完成！"
    fi
    return 0
}
