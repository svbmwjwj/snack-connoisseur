#!/bin/bash
# Snack Connoisseur - 全栈模块化管控中枢 (cnsr)

set -e

COMMAND=$1
SSH_ALIAS=$2
TARGET_IP=$3

# 加载环境变量
if [ -f ".env" ]; then
    source .env
elif [ -f ".env.example" ]; then
    echo "📄 正在从 .env.example 生成本地 .env 文件..."
    cp .env.example .env
    source .env
fi
CNSR_LANG="${CNSR_LANG:-zh}"

function print_usage() {
    local exit_code="${1:-0}"
    if [ "$CNSR_LANG" = "en" ]; then
        cat << 'EOF'
Snack Connoisseur (cnsr) - X-ray REALITY Controller & Anti-Censorship Suite

USAGE:
  ./cnsr.sh <command> [subcommand] [alias] [arguments] [options]

COMMANDS:
  init <alias> [ip]            Provision a new VPS node (Options: --harden, --debug)
  check <alias>                Comprehensive health check: latency, SNI quality, BBR, container
  update <alias>               Full hot-update: sync runner shim, self-healing probe, sanitized env
  rotate sni <alias>           Force fingerprint rotation: reset UUID, keys, ShortID, and SNI (Option: --dry-run)
  rotate dns <alias>           Active domain rotation: refresh secondary domain via Cloudflare
  rotate ip <alias>            Ultimate survival: auto rebind new AWS Lightsail IP & cascade update
  test <alias> [tg]            Push preview sample pack (5 alerts) to Telegram
  lang [zh|en]                 Switch system language (Simplified Chinese zh / English en)
  help                         Show this help manual

OPTIONS:
  -harden, --harden            Enable high-security hardening (for init)
  -debug, --debug              Enable verbose debugging output (for init)
  -dry-run, --dry-run          Simulate SNI evaluation without modifying configuration (for rotate sni)
  -h, --help, -help            Show this help manual

EXAMPLES:
  ./cnsr.sh init sg_aws 198.51.100.1 --harden      # Compound action initialization
  ./cnsr.sh test sg_aws tg                         # Preview Telegram alert samples
  ./cnsr.sh check sg_aws                           # Full node health check
  ./cnsr.sh rotate sni sg_aws --dry-run            # Dry-run SNI rotation
  ./cnsr.sh lang en                                # Switch language to English
EOF
    else
        cat << 'EOF'
Snack Connoisseur (cnsr) - X-ray REALITY 全自动部署与防封锁自愈中枢

用法 / USAGE:
  ./cnsr.sh <命令> [子命令] [别名] [IP] [选项]

核心命令 (COMMANDS):
  init <别名> [IP]             初始化新裸机 (选项: --harden 开启加固, --debug 开启调试)
  check <别名>                 全方位健康体检：延迟测评、SNI质量、内核与容器
  update <别名>                组件全量热更新：平滑同步最新垫片与脱敏凭据
  rotate sni <别名>            强制指纹轮换：重置 UUID、X25519 密钥、ShortID 与 SNI (选项: --dry-run 空转预演)
  rotate dns <别名>            主动域变换：调用 Cloudflare API 刷新大厂内网级二级域名
  rotate ip <别名>             终极断臂求生：AWS Lightsail 自动换绑静态 IP 并自愈
  test <别名> [tg]             向 Telegram 推送全套 5 种通知样张（附带 ⚠️ 标签）
  lang [zh|en]                 切换系统语言 (简体中文 zh / English en)
  help                         显示本帮助手册

参数选项 (OPTIONS):
  --harden                     (用于 init) 启用高强度系统加固与防爆破防火墙
  --debug                      (用于 init) 开启底层详细调试安装日志
  --dry-run                    (用于 rotate sni) 空转预演模式：只嗅探高分域名，不修改配置
  -h, --help                   显示本帮助手册

使用示例 (EXAMPLES):
  ./cnsr.sh init sg_aws 198.51.100.1 --harden    # 复合动作初始化并加固
  ./cnsr.sh test sg_aws tg                       # 触发 Telegram 样张预览
  ./cnsr.sh check sg_aws                         # 全面体检节点质量
  ./cnsr.sh rotate sni sg_aws --dry-run          # 仅评测高分域名
  ./cnsr.sh lang zh                              # 切换系统语言为中文
EOF
    fi
    exit "$exit_code"
}

COMMAND=""
SUBCOMMAND=""
SSH_ALIAS=""
TARGET_IP=""
TARGET_LANG=""
HARDEN_MODE=false
DEBUG_MODE=false
DRY_RUN=false
POSITIONAL=()

# 处理可能是 -h 或 --help 的情况（单独运行脚本或只有-h）
if [ -z "$1" ]; then
    print_usage 1
elif [[ "$1" == "-h" || "$1" == "--help" || "$1" == "-help" || "$1" == "help" ]]; then
    print_usage 0
fi

COMMAND="$1"
shift

# 解析剩余参数
while [ $# -gt 0 ]; do
    case "$1" in
        --harden|-harden)
            HARDEN_MODE=true
            shift
            ;;
        --debug|-debug)
            DEBUG_MODE=true
            shift
            ;;
        --dry-run|-dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help|-help)
            print_usage
            ;;
        -*)
            if [ "$CNSR_LANG" = "en" ]; then
                echo "❌ Error: Unknown flag: $1"
            else
                echo "❌ 错误: 未知参数: $1"
            fi
            exit 1
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

# 如果是需要子命令的，提取 SUBCOMMAND
if [ "$COMMAND" = "rotate" ] || [ "$COMMAND" = "test" ]; then
    if [ ${#POSITIONAL[@]} -gt 0 ]; then
        if [[ "${POSITIONAL[0]}" == "sni" || "${POSITIONAL[0]}" == "dns" || "${POSITIONAL[0]}" == "ip" || "${POSITIONAL[0]}" == "tg" ]]; then
            SUBCOMMAND="${POSITIONAL[0]}"
            POSITIONAL=("${POSITIONAL[@]:1}")
        # 也兼容 cnsr.sh test sg_aws tg 的语序
        elif [[ "${POSITIONAL[1]}" == "tg" && "$COMMAND" == "test" ]]; then
            SUBCOMMAND="tg"
            POSITIONAL=("${POSITIONAL[0]}" "${POSITIONAL[@]:2}")
        fi
    fi
fi

case "$COMMAND" in

    set-lang|lang)
        TARGET_LANG="${TARGET_LANG:-${POSITIONAL[0]}}"
        if [ "$TARGET_LANG" != "zh" ] && [ "$TARGET_LANG" != "en" ]; then
            echo "❌ 错误: 请指定有效语言代码 (zh 或 en)。 (用法: ./cnsr.sh +set-lang <zh|en>)"
            exit 1
        fi
        if [ -f ".env" ]; then
            if grep -q "^CNSR_LANG=" .env; then
                sed -i '' -e "s|^CNSR_LANG=.*|CNSR_LANG=\"$TARGET_LANG\"|g" .env 2>/dev/null || \
                sed -i -e "s|^CNSR_LANG=.*|CNSR_LANG=\"$TARGET_LANG\"|g" .env
            else
                echo "CNSR_LANG=\"$TARGET_LANG\"" >> .env
            fi
        else
            echo "CNSR_LANG=\"$TARGET_LANG\"" > .env
        fi
        if [ "$TARGET_LANG" = "zh" ]; then
            echo "🌐 系统语言已切换为: 简体中文 (zh)"
        else
            echo "🌐 System language switched to: English (en)"
        fi
        exit 0
        ;;
    init)
        if [ -z "$SSH_ALIAS" ] && [ ${#POSITIONAL[@]} -gt 0 ]; then
            if [[ "${POSITIONAL[0]}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                TARGET_IP="${POSITIONAL[0]}"
                [ ${#POSITIONAL[@]} -gt 1 ] && SSH_ALIAS="${POSITIONAL[1]}"
            else
                SSH_ALIAS="${POSITIONAL[0]}"
                [ ${#POSITIONAL[@]} -gt 1 ] && [ -z "$TARGET_IP" ] && TARGET_IP="${POSITIONAL[1]}"
            fi
        fi
        if [ -z "$SSH_ALIAS" ]; then
            if [ "$CNSR_LANG" = "en" ]; then echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh init <alias> [ip])"; else echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh init <别名> [IP])"; fi
            exit 1
        fi
        ;;
    check|update|rotate|test)
        if [ -z "$SSH_ALIAS" ] && [ ${#POSITIONAL[@]} -gt 0 ]; then
            SSH_ALIAS="${POSITIONAL[0]}"
        fi
        if [ -z "$SSH_ALIAS" ]; then
            if [ "$CNSR_LANG" = "en" ]; then echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh $COMMAND <alias>)"; else echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh $COMMAND <别名>)"; fi
            exit 1
        fi
        ;;
    *)
        print_usage
        ;;
esac

# ==========================================
# 核心公共函数
# ==========================================

function select_menu() {
    local prompt="$1"
    shift
    local options=("$@")
    local cur=0
    local count=${#options[@]}
    local key=""
    local subkey=""

    if [ ! -t 0 ]; then
        MENU_CHOICE=1
        return
    fi

    # 隐藏光标并在退出时恢复
    tput civis 2>/dev/null || printf "\033[?25l"
    trap 'tput cnorm 2>/dev/null || printf "\033[?25h"' RETURN EXIT INT TERM

    echo ""
    echo "$prompt"

    # 先输出占位行，确保向上回滚时光标稳定
    for i in "${!options[@]}"; do
        echo ""
    done

    while true; do
        # 向上移动 count 行重绘当前选项状态
        printf "\033[%dA" "$count"

        for i in "${!options[@]}"; do
            if [ "$i" -eq "$cur" ]; then
                printf "\033[1;36m  ➜ %s\033[0m\033[K\n" "${options[$i]}"
            else
                printf "    \033[90m%s\033[0m\033[K\n" "${options[$i]}"
            fi
        done

        IFS= read -rsn1 key
        if [ "$key" = $'\x1b' ]; then
            read -rsn2 -t 1 subkey 2>/dev/null || true
            if [[ "$subkey" == "[A" || "$subkey" == "OA" ]]; then
                cur=$(( (cur - 1 + count) % count ))
            elif [[ "$subkey" == "[B" || "$subkey" == "OB" ]]; then
                cur=$(( (cur + 1) % count ))
            fi
        elif [ "$key" = "" ]; then
            break
        elif [[ "$key" =~ ^[1-9]$ ]] && [ "$key" -le "$count" ]; then
            cur=$((key - 1))
            break
        elif [ "$key" = "k" ] || [ "$key" = "K" ]; then
            cur=$(( (cur - 1 + count) % count ))
        elif [ "$key" = "j" ] || [ "$key" = "J" ]; then
            cur=$(( (cur + 1) % count ))
        fi
    done

    # 选定确认后：光标移回到标题所在行，抹除下方全部未选选项，仅保留精简的已选结论
    printf "\033[%dA" "$((count + 1))"
    local short_prompt=$(echo "$prompt" | sed -E 's/[[:space:]]*\(.*//g')
    printf "\033[1;32m✔\033[0m %s: \033[1;36m%s\033[0m\033[K\n" "$short_prompt" "${options[$cur]}"
    for ((i=0; i<count; i++)); do
        printf "\033[K\n"
    done
    printf "\033[%dA" "$count"

    tput cnorm 2>/dev/null || printf "\033[?25h"
    MENU_CHOICE=$((cur + 1))
}

function guess_default_user() {
    local alias_lower=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    
    # 操作系统关键词优先
    if [[ "$alias_lower" =~ (ubuntu) ]]; then
        echo "ubuntu"
    elif [[ "$alias_lower" =~ (debian) ]]; then
        echo "admin"
    elif [[ "$alias_lower" =~ (centos|rocky|alma|amzn|ec2) ]]; then
        echo "ec2-user"
    elif [[ "$alias_lower" =~ (opc) ]]; then
        echo "opc"
    # 云厂商关键词推断
    elif [[ "$alias_lower" =~ (aws|lightsail|ls) ]]; then
        echo "admin"
    elif [[ "$alias_lower" =~ (oci|oracle) ]]; then
        echo "ubuntu"
    elif [[ "$alias_lower" =~ (gcp|google) ]]; then
        echo "admin"
    else
        echo "root"
    fi
}

function get_real_host() {
    local host=$(ssh -G "$SSH_ALIAS" 2>/dev/null | awk '/^hostname / {print $2}')
    if [ -z "$host" ]; then
        host="$SSH_ALIAS"
    fi
    echo "$host"
}

function detect_remote_ipv6() {
    local target="$1"
    ssh -o BatchMode=yes "$target" "
        ip=\$(hostname -I 2>/dev/null | tr ' ' '\n' | grep ':' | grep -v '^fe80' | grep -v '^fc' | grep -v '^fd' | head -n1)
        if [ -z \"\$ip\" ]; then
            ip=\$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/ {print \$2}' | cut -d/ -f1 | grep -v '^fe80' | grep -v '^fc' | grep -v '^fd' | head -n1)
        fi
        if [ -z \"\$ip\" ]; then
            mac=\$(curl -s --connect-timeout 2 http://169.254.169.254/latest/meta-data/mac 2>/dev/null || true)
            [ -n \"\$mac\" ] && ip=\$(curl -s --connect-timeout 2 \"http://169.254.169.254/latest/meta-data/network/interfaces/macs/\${mac}/ipv6s\" 2>/dev/null | head -n1 || true)
        fi
        if [ -z \"\$ip\" ]; then
            ip=\$(curl -6 -s --connect-timeout 3 https://api64.ipify.org 2>/dev/null || curl -6 -s --connect-timeout 3 https://icanhazip.com 2>/dev/null || echo 'none')
        fi
        [ -z \"\$ip\" ] && ip='none'
        echo \"\$ip\"
    " 2>/dev/null || echo "none"
}

function upgrade_ssh_config_hostname() {
    local alias=$1
    local domain=$2
    if [ -z "$domain" ] || [ "$domain" = "null" ]; then return; fi

    echo "🔍 正在对本地网络校验新生成域名的 DNS 解析状态 ($domain)..."
    local resolved_ip=""
    for i in {1..5}; do
        resolved_ip=$(python3 -c "import socket; print(socket.gethostbyname('$domain'))" 2>/dev/null || true)
        if [ -n "$resolved_ip" ]; then break; fi
        sleep 1
    done

    if [ -n "$resolved_ip" ]; then
        ssh-keygen -R "$domain" >/dev/null 2>&1 || true
        python3 -c "
import sys, re
config_path = '$HOME/.ssh/config'
alias = '$alias'
domain = '$domain'
try:
    with open(config_path, 'r') as f:
        content = f.read()
    pattern = r'(Host\s+' + re.escape(alias) + r'\s+[\s\S]*?HostName\s+)(\S+)'
    new_content = re.sub(pattern, r'\g<1>' + domain, content, count=1)
    with open(config_path, 'w') as f:
        f.write(new_content)
    print('🌐 已成功将 ~/.ssh/config 中的 HostName 升级为伪装域名:', domain)
except Exception as e:
    pass
" 2>/dev/null || true
    else
        echo "ℹ️ 新伪装域名 ($domain) 全网 DNS 广播扩散中，当前本地 SSH Config 维持 IP 连接以保障稳定。"
    fi
}

function check_cf_credentials() {
    if [ -z "$CF_API_TOKEN" ]; then
        read -p "⚠️ 请输入您的 Cloudflare API Token: " CF_API_TOKEN
        echo "CF_API_TOKEN=\"$CF_API_TOKEN\"" >> .env
    fi
    if [ -z "$CF_ZONE_ID" ]; then
        read -p "⚠️ 请输入您的 Cloudflare Zone ID: " CF_ZONE_ID
        echo "CF_ZONE_ID=\"$CF_ZONE_ID\"" >> .env
    fi
}

function check_aws_credentials() {
    if ! command -v aws &> /dev/null; then
        echo "⚠️ AWS CLI 未安装！正在尝试为您全平台自动安装..."
        local OS=$(uname -s)
        local ARCH=$(uname -m)
        local TMP_DIR=$(mktemp -d)
        
        pushd "$TMP_DIR" > /dev/null
        if [ "$OS" = "Darwin" ]; then
            echo "📦 检测到 macOS，正在下载官方 pkg 安装包..."
            curl -s "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg"
            sudo installer -pkg AWSCLIV2.pkg -target /
        elif [ "$OS" = "Linux" ]; then
            echo "📦 检测到 Linux ($ARCH)，正在下载官方安装包..."
            if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
                curl -s "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"
            else
                curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
            fi
            
            if ! command -v unzip &> /dev/null; then
                if command -v apt-get &> /dev/null; then sudo apt-get update && sudo apt-get install -y unzip; 
                elif command -v yum &> /dev/null; then sudo yum install -y unzip; 
                elif command -v apk &> /dev/null; then sudo apk add unzip; fi
            fi
            
            unzip -q awscliv2.zip
            sudo ./aws/install
        else
            echo "❌ 暂不支持的操作系统: $OS。请手动安装: https://aws.amazon.com/cli/"
            exit 1
        fi
        popd > /dev/null
        rm -rf "$TMP_DIR"
        
        if ! command -v aws &> /dev/null; then
            echo "❌ 自动安装似乎失败了，找不到 aws 命令，请手动安装。"
            exit 1
        fi
        echo "✅ AWS CLI 自动安装成功！"
    fi

}

function generate_cf_domain() {
    local ip_v4="$1"
    local ip_v6="$2"
    local alias="$3"
    
    check_cf_credentials
    echo "🌐 正在生成大厂风格伪装域名..."
    local PREFIX_LIST=("api-gateway" "auth-svc" "user-metrics" "static-cdn" "edge-cache" "log-stream")
    local SUFFIX=$((RANDOM % 900 + 100))
    local CF_SUBDOMAIN="${PREFIX_LIST[$RANDOM % ${#PREFIX_LIST[@]}]}-v${SUFFIX}"

    local CF_DOMAIN_RESPONSE=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H "Content-Type: application/json")
    local CF_BASE_DOMAIN=$(echo "$CF_DOMAIN_RESPONSE" | jq -r '.result.name')
    if [ "$CF_BASE_DOMAIN" = "null" ] || [ -z "$CF_BASE_DOMAIN" ]; then
        echo "❌ 无法获取 Cloudflare 域名，请检查 Token 或 Zone ID！"
        exit 1
    fi

    local FULL_DOMAIN="${CF_SUBDOMAIN}.${CF_BASE_DOMAIN}"
    echo "✅ 生成伪装域名: $FULL_DOMAIN"

    echo "🧹 正在清理当前服务器历史冗余解析记录 (匹配 IP 或别名 $alias)..."
    local OLD_A_IDS=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records?per_page=100" \
        -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" | jq -r --arg ip "$ip_v4" --arg al "$alias" '.result[]? | select(.type=="A" and (.content==$ip or .comment==$al)) | .id' 2>/dev/null)
    for id in $OLD_A_IDS; do
        curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records/$id" \
            -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" > /dev/null
    done

    echo "☁️ 正在通过 Cloudflare API 创建最新 A 记录..."
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H "Content-Type: application/json" \
        --data '{"type":"A","name":"'"$CF_SUBDOMAIN"'","content":"'"$ip_v4"'","ttl":1,"proxied":false,"comment":"'"$alias"'"}' > /dev/null
    
    if [ "$ip_v6" != "none" ] && [ -n "$ip_v6" ]; then
        local OLD_AAAA_IDS=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records?per_page=100" \
            -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" | jq -r --arg ip "$ip_v6" --arg al "$alias" '.result[]? | select(.type=="AAAA" and (.content==$ip or .comment==$al)) | .id' 2>/dev/null)
        for id in $OLD_AAAA_IDS; do
            curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records/$id" \
                -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" > /dev/null
        done

        echo "☁️ 正在通过 Cloudflare API 创建最新 AAAA 记录..."
        curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \
            -H "Authorization: Bearer $CF_API_TOKEN" \
            -H "Content-Type: application/json" \
            --data '{"type":"AAAA","name":"'"$CF_SUBDOMAIN"'","content":"'"$ip_v6"'","ttl":1,"proxied":false,"comment":"'"$alias"'"}' > /dev/null
    fi
    echo "$FULL_DOMAIN"
}

# ==========================================
# 环境变量脱敏与服务端组件同步
# ==========================================

function sanitize_env_for_node() {
    local out_file="$1"
    if [ -z "$out_file" ]; then
        echo "❌ 错误: sanitize_env_for_node 需要指定输出文件路径。"
        return 1
    fi
    rm -f "$out_file"
    touch "$out_file"

    # 严格白名单下发至远端节点：绝不包含 AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, CF_API_TOKEN, CF_ZONE_ID
    # 若配置了 GATEWAY_URL，则开启纯零凭据模式（节点不保留任何 TG/GH 敏感 Token）
    local allowed_keys=()
    if [ -n "$GATEWAY_URL" ]; then
        allowed_keys=("GATEWAY_URL" "GATEWAY_AUTH_KEY" "DEFAULT_CLOUD_USER" "CNSR_LANG")
    else
        allowed_keys=("TG_BOT_TOKEN" "TG_CHAT_ID" "GH_TOKEN" "DEFAULT_CLOUD_USER" "CNSR_LANG")
    fi
    for key in "${allowed_keys[@]}"; do
        local val="${!key}"
        if [ -n "$val" ]; then
            echo "${key}=\"${val}\"" >> "$out_file"
        fi
    done

    # 双重安全防护断言
    if grep -E "^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|CF_API_TOKEN|CF_ZONE_ID)=" "$out_file" 2>/dev/null; then
        echo "❌ 严重安全告警: 脱敏环境配置中检测到高危云凭据，已强制拦截！"
        rm -f "$out_file"
        return 1
    fi
}

function sync_node_scripts() {
    local alias="$1"
    local override_ip="$2"
    local override_host="$3"
    
    if [ -z "$alias" ]; then
        echo "❌ 错误: sync_node_scripts 需要指定节点别名。"
        return 1
    fi

    echo "🔄 正在准备为节点 [$alias] 同步最新组件与脱敏凭据..."

    # 1. 探测/解析远端用户信息与目录
    local REMOTE_HOME=$(ssh -o BatchMode=yes "$alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"

    # 2. 探测 IP / IPv6 / CIDR / 伪装域名
    local IPV4="$override_ip"
    if [ -z "$IPV4" ]; then
        IPV4=$(ssh -o BatchMode=yes "$alias" "curl -4 -s --connect-timeout 5 ifconfig.me || curl -4 -s --connect-timeout 5 api.ipify.org" 2>/dev/null || true)
    fi
    if [ -z "$IPV4" ]; then
        IPV4=$(ssh -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
        [ -z "$IPV4" ] && IPV4="$alias"
    fi

    local IPV4_CIDR="none"
    if [[ "$IPV4" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        IFS=. read -r a b c d <<< "$IPV4"
        local C_BASE=$(( (c / 4) * 4 ))
        IPV4_CIDR="${a}.${b}.${C_BASE}.0/22"
    fi

    local IPV6=$(detect_remote_ipv6 "$alias")
    local IPV6_CIDR="none"
    if [ "$IPV6" != "none" ] && [ -n "$IPV6" ]; then
        IPV6_CIDR=$(python3 -c "import sys, ipaddress; print(str(ipaddress.IPv6Network(f'{sys.argv[1]}/112', strict=False)))" "$IPV6" 2>/dev/null || echo "none")
        if [ -z "$IPV6_CIDR" ] || [ "$IPV6_CIDR" = "none" ]; then
            local V6_PREFIX=$(echo "$IPV6" | awk -F':' '{print $1":"$2":"$3":"$4}')
            [ -n "$V6_PREFIX" ] && IPV6_CIDR="${V6_PREFIX}::/64"
        fi
    fi

    # 获取真实/已有的伪装域名 (若传入 override_host 则优先使用，若远端已有则继承，否则读取 SSH HostName)
    local REAL_HOST="$override_host"
    if [ -z "$REAL_HOST" ]; then
        local REMOTE_SERVER_HOST=$(ssh -o BatchMode=yes "$alias" "grep -E '^SERVER_HOST=' ${DOCKER_APP_DIR}/reality_rotate.sh 2>/dev/null | cut -d'\"' -f2" 2>/dev/null || true)
        if [ -n "$REMOTE_SERVER_HOST" ] && [ "$REMOTE_SERVER_HOST" != "PLACEHOLDER_HOST" ]; then
            REAL_HOST="$REMOTE_SERVER_HOST"
        else
            REAL_HOST=$(ssh -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
            [ -z "$REAL_HOST" ] && REAL_HOST="$alias"
        fi
    fi

    # 3. 创建本地临时渲染目录
    local TMP_SYNC_DIR=$(mktemp -d)

    # 3.1 渲染 runner.sh
    cp templates/runner.template.sh "$TMP_SYNC_DIR/runner.sh"
    sed -i '' -e "s|TARGET_IP=\"PLACEHOLDER_IP\"|TARGET_IP=\"$IPV4\"|g" \
              -e "s|SERVER_HOST=\"PLACEHOLDER_HOST\"|SERVER_HOST=\"$REAL_HOST\"|g" \
              -e "s|SSH_ALIAS=\"PLACEHOLDER_ALIAS\"|SSH_ALIAS=\"$alias\"|g" \
              -e "s|TARGET_CIDR_V4=\"PLACEHOLDER_CIDR_V4\"|TARGET_CIDR_V4=\"$IPV4_CIDR\"|g" \
              -e "s|TARGET_CIDR_V6=\"PLACEHOLDER_CIDR_V6\"|TARGET_CIDR_V6=\"$IPV6_CIDR\"|g" \
              -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/runner.sh" 2>/dev/null || \
    sed -i -e "s|TARGET_IP=\"PLACEHOLDER_IP\"|TARGET_IP=\"$IPV4\"|g" \
           -e "s|SERVER_HOST=\"PLACEHOLDER_HOST\"|SERVER_HOST=\"$REAL_HOST\"|g" \
           -e "s|SSH_ALIAS=\"PLACEHOLDER_ALIAS\"|SSH_ALIAS=\"$alias\"|g" \
           -e "s|TARGET_CIDR_V4=\"PLACEHOLDER_CIDR_V4\"|TARGET_CIDR_V4=\"$IPV4_CIDR\"|g" \
           -e "s|TARGET_CIDR_V6=\"PLACEHOLDER_CIDR_V6\"|TARGET_CIDR_V6=\"$IPV6_CIDR\"|g" \
           -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/runner.sh"

    # 3.2 渲染 reality_rotate.sh
    cp templates/reality_rotate.template.sh "$TMP_SYNC_DIR/reality_rotate.sh"
    sed -i '' -e "s|PLACEHOLDER_IP|$IPV4|g" \
              -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
              -e "s|PLACEHOLDER_ALIAS|$alias|g" \
              -e "s|PLACEHOLDER_CIDR_V4|$IPV4_CIDR|g" \
              -e "s|PLACEHOLDER_CIDR_V6|$IPV6_CIDR|g" \
              -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_rotate.sh" 2>/dev/null || \
    sed -i -e "s|PLACEHOLDER_IP|$IPV4|g" \
           -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
           -e "s|PLACEHOLDER_ALIAS|$alias|g" \
           -e "s|PLACEHOLDER_CIDR_V4|$IPV4_CIDR|g" \
           -e "s|PLACEHOLDER_CIDR_V6|$IPV6_CIDR|g" \
           -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_rotate.sh"

    # 3.3 渲染 reality_check.sh
    cp templates/reality_check.template.sh "$TMP_SYNC_DIR/reality_check.sh"
    sed -i '' -e "s|PLACEHOLDER_IPV6|$IPV6|g" \
              -e "s|PLACEHOLDER_IP|$IPV4|g" \
              -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
              -e "s|PLACEHOLDER_ALIAS|$alias|g" \
              -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_check.sh" 2>/dev/null || \
    sed -i -e "s|PLACEHOLDER_IPV6|$IPV6|g" \
           -e "s|PLACEHOLDER_IP|$IPV4|g" \
           -e "s|PLACEHOLDER_HOST|$REAL_HOST|g" \
           -e "s|PLACEHOLDER_ALIAS|$alias|g" \
           -e "s|/home/admin/docker-apps/xray|${DOCKER_APP_DIR}|g" "$TMP_SYNC_DIR/reality_check.sh"

    # 3.4 准备 tg_templates.sh
    cp templates/tg_templates.sh "$TMP_SYNC_DIR/tg_templates.sh"

    # 3.5 准备脱敏 .env
    sanitize_env_for_node "$TMP_SYNC_DIR/.env"

    # 4. 本地静态语法校验 (bash -n)
    for script in runner.sh reality_rotate.sh reality_check.sh tg_templates.sh; do
        if ! bash -n "$TMP_SYNC_DIR/$script" >/dev/null 2>&1; then
            echo "❌ 严重错误: 生成的脚本 $script 本地语法检查 (bash -n) 失败！中断推送。"
            rm -rf "$TMP_SYNC_DIR"
            return 1
        fi
    done

    # 5. 上传至远端服务器
    local SCP_OPT="-q"
    ssh -o BatchMode=yes "$alias" "mkdir -p ${DOCKER_APP_DIR}/conf"
    scp $SCP_OPT "$TMP_SYNC_DIR/runner.sh" "$alias":${DOCKER_APP_DIR}/runner.sh
    scp $SCP_OPT "$TMP_SYNC_DIR/reality_rotate.sh" "$alias":${DOCKER_APP_DIR}/reality_rotate.sh
    scp $SCP_OPT "$TMP_SYNC_DIR/reality_check.sh" "$alias":${DOCKER_APP_DIR}/reality_check.sh
    scp $SCP_OPT "$TMP_SYNC_DIR/tg_templates.sh" "$alias":${DOCKER_APP_DIR}/tg_templates.sh
    scp $SCP_OPT "$TMP_SYNC_DIR/.env" "$alias":${DOCKER_APP_DIR}/.env

    if [ -f "tools/reality-checker" ]; then
        scp $SCP_OPT tools/reality-checker "$alias":${DOCKER_APP_DIR}/reality-checker
    fi
    if [ -f "fallback_snis.txt" ]; then
        scp $SCP_OPT fallback_snis.txt "$alias":${DOCKER_APP_DIR}/fallback_snis.txt
    fi

    # 6. 赋予执行权限并挂载/刷新 Crontab 为 runner.sh
    ssh -o BatchMode=yes "$alias" "
        chmod +x ${DOCKER_APP_DIR}/runner.sh ${DOCKER_APP_DIR}/reality_rotate.sh ${DOCKER_APP_DIR}/reality_check.sh ${DOCKER_APP_DIR}/tg_templates.sh 2>/dev/null || true
        [ -f ${DOCKER_APP_DIR}/reality-checker ] && chmod +x ${DOCKER_APP_DIR}/reality-checker 2>/dev/null || true
        if ! command -v crontab >/dev/null 2>&1; then if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -y && sudo apt-get install -y cron; fi; fi
        (sudo -u \$(whoami) crontab -l 2>/dev/null | grep -v -E 'reality_rotate.sh|runner.sh'; echo '*/15 * * * * ${DOCKER_APP_DIR}/runner.sh > ${DOCKER_APP_DIR}/rotate.log 2>&1') | sudo -u \$(whoami) crontab -
    "

    rm -rf "$TMP_SYNC_DIR"
    echo "✅ 节点 [$alias] 组件与脱敏凭据同步对齐完成！"
}

# ==========================================
# 模块：INIT
# ==========================================
function module_init() {
    local SECURE_MODE="${HARDEN_MODE:-false}"
    local DEBUG_MODE="${DEBUG_MODE:-false}"
    
    for arg in "$@"; do
        if [ "$arg" = "--harden" ] || [ "$arg" = "-harden" ]; then
            SECURE_MODE="true"
        elif [ "$arg" = "--debug" ] || [ "$arg" = "-debug" ]; then
            DEBUG_MODE="true"
        fi
    done

    # 兼容原逻辑，如果 TARGET_IP 原本是 --harden 或者是 --debug，则清空
    if [[ "$TARGET_IP" == --* ]]; then
        TARGET_IP=""
    fi

    echo "🚀 开始初始化新节点: $SSH_ALIAS"

    local DETECTED_USER=""
    if [ -n "$TARGET_IP" ]; then
        local SUGGESTED_USER=$(guess_default_user "$SSH_ALIAS")
        INPUT_USER=""
        if [ -t 0 ]; then
            if [ "$CNSR_LANG" = "en" ]; then
                echo "💡 Detected alias [$SSH_ALIAS], inferred default username: $SUGGESTED_USER"
                read -p "👤 Enter initial username [Default: $SUGGESTED_USER] (Press Enter to confirm): " INPUT_USER
            else
                echo "💡 检测到别名 [$SSH_ALIAS]，推测默认初始用户名: $SUGGESTED_USER"
                read -p "👤 请输入初始用户名 [默认: $SUGGESTED_USER] (直接回车确认): " INPUT_USER
            fi
            INPUT_USER=$(echo "$INPUT_USER" | tr -d '\r\n\t' | xargs)
            if [ -z "$INPUT_USER" ]; then
                INPUT_USER="$SUGGESTED_USER"
            fi
        else
            INPUT_USER="$SUGGESTED_USER"
        fi
        DETECTED_USER="$INPUT_USER"
        if [ "$CNSR_LANG" = "en" ]; then
            echo "   🎯 Selected initial username: $DETECTED_USER"
        else
            echo "   🎯 已采用初始用户名: $DETECTED_USER"
        fi
        
        # 自动清除本地 possible MITM 残留（如果这个 IP 以前被用过）
        ssh-keygen -R "$TARGET_IP" >/dev/null 2>&1 || true

        local FINAL_IDENTITY_FILE=""
        local FINAL_PUB_KEY=""

        local AUTH_MAIN_CHOICE="1"
        if [ -t 0 ]; then
            select_menu "🔑 请选择初始认证方式 (使用 ↑/↓ 方向键切换，按 Enter 确认):" \
                "密钥登录 / SSH Agent (使用已有私钥、Agent 托管密钥或现场新建密钥)" \
                "密码登录 (通过初始密码连接并自动向服务器注入本节点公钥)"
            AUTH_MAIN_CHOICE="$MENU_CHOICE"
        fi

        if [ "$AUTH_MAIN_CHOICE" = "1" ]; then
            local KEY_SRC_CHOICE="1"
            if [ -t 0 ]; then
                select_menu "🔐 请选择您的密钥来源 (使用 ↑/↓ 方向键切换，按 Enter 确认):" \
                    "SSH Agent 托管密钥 (Bitwarden / 1Password / 系统 Agent)" \
                    "本地已有私钥文件 (如 ~/.ssh/id_ed25519、~/.ssh/id_rsa 或云厂商 .pem)" \
                    "为此节点现场生成全新专属密钥对 (自动生成 Ed25519 密钥对)"
                KEY_SRC_CHOICE="$MENU_CHOICE"
            fi

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
                    LOCAL_KEY_PATH=$(eval echo "$LOCAL_KEY_PATH")

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
                        eval TEMP_PEM="$TEMP_PEM"
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

        echo "   -> 正在使用身份 [$DETECTED_USER] 进行免密握手探活..."
        local IDENTITY_OPT=()
        if [ -n "$FINAL_IDENTITY_FILE" ] && [ -f "$FINAL_IDENTITY_FILE" ]; then
            IDENTITY_OPT=(-i "$FINAL_IDENTITY_FILE" -o IdentitiesOnly=yes)
        fi

        if ssh -o BatchMode=yes -o StrictHostKeyChecking=no "${IDENTITY_OPT[@]}" "$DETECTED_USER@$TARGET_IP" "echo 'alive'" >/dev/null 2>&1; then
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
            python3 -c "
import sys, re
config_path = '$HOME/.ssh/config'
alias = '$SSH_ALIAS'
ip = '$TARGET_IP'
user = '$DETECTED_USER'
identity_line = '$CONFIG_IDENTITY_VAL'
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
        IPV6_CIDR=$(python3 -c "import sys, ipaddress; print(str(ipaddress.IPv6Network(f'{sys.argv[1]}/112', strict=False)))" "$IPV6" 2>/dev/null)
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
    
    local SCP_OPT=""
    if [ "$DEBUG_MODE" = "false" ]; then SCP_OPT="-q"; fi

    echo "🛡️ 正在装配并同步节点基础组件 (runner/自愈/体检/脱敏凭据)..."
    sync_node_scripts "$SSH_ALIAS" "$IPV4" "$FULL_DOMAIN"

    ssh "$SSH_ALIAS" "[ -f ${DOCKER_APP_DIR}/compose.yml ]" || scp $SCP_OPT templates/compose.yml.template "$SSH_ALIAS":${DOCKER_APP_DIR}/compose.yml
    ssh "$SSH_ALIAS" "[ -f ${DOCKER_APP_DIR}/conf/config.json ]" || scp $SCP_OPT templates/config.json.template "$SSH_ALIAS":${DOCKER_APP_DIR}/conf/config.json

    local SHADOW_USER=""
    local NEW_SSH_PORT="22"

    if [ "$SECURE_MODE" = "true" ]; then
        echo "⚙️ 正在执行系统加固及 Docker 部署 (深层 Harden 模式)..."
        
        # 1. 影子管理员词库 (科幻名)
        local SCIFI_NAMES=("arrakis" "replicant" "skynet" "gotham" "trantor" "jedi" "cyberdyne" "matrix" "nostromo" "tesseract")
        SHADOW_USER=${SCIFI_NAMES[$RANDOM % ${#SCIFI_NAMES[@]}]}
        NEW_SSH_PORT=$((RANDOM % 50000 + 10000))
        
        echo "   -> [加固] 分配高危混淆端口: $NEW_SSH_PORT"
        echo "   -> [加固] 创建影子管理员账号: $SHADOW_USER"

          ssh "$SSH_ALIAS" "
          set -e
          sudo apt-get update -y >/dev/null 2>&1 && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ufw fail2ban jq cron curl iputils-ping dnsutils >/dev/null 2>&1
          
          # 创建科幻随机账户并配置免密 sudo
          if ! id -u $SHADOW_USER > /dev/null 2>&1; then
              sudo useradd -m -s /bin/bash $SHADOW_USER
              echo \"$SHADOW_USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/90-$SHADOW_USER >/dev/null
              sudo mkdir -p /home/$SHADOW_USER/.ssh
              if [ -f ~/.ssh/authorized_keys ]; then
                  sudo cp ~/.ssh/authorized_keys /home/$SHADOW_USER/.ssh/
              else
                  sudo touch /home/$SHADOW_USER/.ssh/authorized_keys
              fi
              sudo chown -R $SHADOW_USER:$SHADOW_USER /home/$SHADOW_USER/.ssh
              sudo chmod 700 /home/$SHADOW_USER/.ssh
              sudo chmod 600 /home/$SHADOW_USER/.ssh/authorized_keys
          fi

          # UFW 隔离
          sudo ufw default deny incoming >/dev/null 2>&1
          sudo ufw default allow outgoing >/dev/null 2>&1
          sudo ufw allow 22/tcp >/dev/null 2>&1
          sudo ufw allow $NEW_SSH_PORT/tcp >/dev/null 2>&1
          sudo ufw allow 443/tcp >/dev/null 2>&1
          sudo ufw --force enable >/dev/null 2>&1
          sudo systemctl enable --now fail2ban >/dev/null 2>&1
          
          # 封杀默认账户及 SSH 端口移位
          sudo sed -i 's/^#*Port .*/Port $NEW_SSH_PORT/' /etc/ssh/sshd_config
          sudo sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
          sudo sed -i 's/^#*PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
          
          # 拒绝除了新账户之外的其他默认用户
          echo \"AllowUsers $SHADOW_USER\" | sudo tee -a /etc/ssh/sshd_config >/dev/null
          
          # 目录迁移与权限调整 (在断开 sshd 之前执行)
          sudo mkdir -p /home/$SHADOW_USER/docker-apps
          sudo mv ${DOCKER_APP_DIR} /home/$SHADOW_USER/docker-apps/
          sudo chown -R $SHADOW_USER:$SHADOW_USER /home/$SHADOW_USER/docker-apps
          
          # 更新脚本内部的硬编码路径
          sudo sed -i 's|${DOCKER_APP_DIR}|/home/$SHADOW_USER/docker-apps/xray|g' /home/$SHADOW_USER/docker-apps/xray/reality_rotate.sh
          sudo sed -i 's|${DOCKER_APP_DIR}|/home/$SHADOW_USER/docker-apps/xray|g' /home/$SHADOW_USER/docker-apps/xray/reality_check.sh
          sudo sed -i 's|${DOCKER_APP_DIR}|/home/$SHADOW_USER/docker-apps/xray|g' /home/$SHADOW_USER/docker-apps/xray/runner.sh
          
          sudo systemctl restart sshd
          sudo ufw delete allow 22/tcp >/dev/null 2>&1 || true
        "
        DOCKER_APP_DIR="/home/$SHADOW_USER/docker-apps/xray"
        
        echo "   -> [加固] 本地环境修正，适配新用户与端口..."
        # 修正 ~/.ssh/config 里的用户和端口
        python3 -c "
import sys, re
config_path = '$HOME/.ssh/config'
alias = '$SSH_ALIAS'
user = '$SHADOW_USER'
port = '$NEW_SSH_PORT'
try:
    with open(config_path, 'r') as f:
        content = f.read()
    block_pattern = r'(Host\s+' + re.escape(alias) + r'\b(.*?))(?=\nHost\s|\Z)'
    match = re.search(block_pattern, content, flags=re.DOTALL)
    if match:
        block = match.group(1)
        block = re.sub(r'(^[ \t]*User\s+)(\S+)', r'\g<1>' + user, block, flags=re.MULTILINE)
        if re.search(r'^[ \t]*Port\s+', block, flags=re.MULTILINE):
            block = re.sub(r'(^[ \t]*Port\s+)(\d+)', r'\g<1>' + port, block, flags=re.MULTILINE)
        else:
            block = block.rstrip() + f'\n    Port {port}\n'
        new_content = content[:match.start()] + block + content[match.end():]
        with open(config_path, 'w') as f:
            f.write(new_content)
except Exception as e:
    pass
" 2>/dev/null || true

        echo "   -> [加固] 尝试通过 AWS CLI 为 Lightsail 实例放行云端防火墙端口 $NEW_SSH_PORT..."
        if command -v aws >/dev/null 2>&1; then
            local AWS_REGION_PROBE=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -s -m 2 http://169.254.169.254/latest/meta-data/placement/region" 2>/dev/null || true)
            if [ -n "$AWS_REGION_PROBE" ]; then
                local INSTANCE_NAME_PROBE=$(aws lightsail get-instances --region "$AWS_REGION_PROBE" 2>/dev/null | jq -r ".instances[] | select(.publicIpAddress == \"$TARGET_IP\") | .name" 2>/dev/null || true)
                if [ -n "$INSTANCE_NAME_PROBE" ]; then
                    aws lightsail open-instance-port --instance-name "$INSTANCE_NAME_PROBE" --region "$AWS_REGION_PROBE" --port-info fromPort=$NEW_SSH_PORT,toPort=$NEW_SSH_PORT,protocol=TCP >/dev/null 2>&1 || true
                    echo "   ✅ 已自动调用 AWS API 在 Lightsail 云防火墙中放行 $NEW_SSH_PORT 端口。"
                fi
            fi
        fi

        echo "✅ 深度安全加固完成！新身份已隐匿潜伏。"
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
    
    scp $SCP_OPT async_deploy.sh "$SSH_ALIAS":${DOCKER_APP_DIR}/async_deploy.sh
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

# ==========================================
# 模块：ROTATE-XRAY
# ==========================================
function module_rotate_xray() {
    if [ "$DRY_RUN" = "true" ]; then
        echo "🧪 模拟 Dry-Run 纯选域演练..."
    else
        echo "🔄 强制触发 X-ray 指纹轮换..."
    fi
    sync_node_scripts "$SSH_ALIAS"
    
    local REMOTE_HOME=$(ssh "$SSH_ALIAS" "eval echo ~\$USER")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"
    
    local cmd_prefix=""
    if [ "$DRY_RUN" = "true" ]; then
        cmd_prefix="IS_TEST_MODE=1 DRY_RUN=1"
    fi

    ssh -t "$SSH_ALIAS" "
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
    if [ "$DRY_RUN" != "true" ]; then
        echo "✅ 指纹轮换完毕，新的 VLESS 节点信息已推送到 Telegram！"
    fi
}

# ==========================================
# 模块：ROTATE-DNS
# ==========================================
function module_rotate_dns() {
    check_cf_credentials
    echo "🌐 开始洗白 DNS 域名矩阵..."
    
    local IPV4=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -4 -s ifconfig.me")
    local IPV6=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -6 -s ifconfig.me || echo 'none'")
    
    local FULL_DOMAIN=$(generate_cf_domain "$IPV4" "$IPV6" "$SSH_ALIAS" | tail -n 1)
    
    echo "🔧 正在将新域名 $FULL_DOMAIN 下发至服务器配置中..."
    local REMOTE_HOME=$(ssh "$SSH_ALIAS" "eval echo ~\$USER")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"
    
    ssh "$SSH_ALIAS" "
        set -e
        sed -i 's/^SERVER_HOST=.*/SERVER_HOST=\"$FULL_DOMAIN\"/' ${DOCKER_APP_DIR}/reality_rotate.sh
        sed -i 's/^SERVER_HOST=.*/SERVER_HOST=\"$FULL_DOMAIN\"/' ${DOCKER_APP_DIR}/reality_check.sh 2>/dev/null || true
        sed -i 's/^SERVER_HOST=.*/SERVER_HOST=\"$FULL_DOMAIN\"/' ${DOCKER_APP_DIR}/runner.sh 2>/dev/null || true
    "
    upgrade_ssh_config_hostname "$SSH_ALIAS" "$FULL_DOMAIN"
    echo "✅ 伪装域名已更新，下一次 cron 将自动使用新域名！您也可以执行 ./cnsr.sh rotate sni $SSH_ALIAS 强制立刻生效。"
}

# ==========================================
# 模块：ROTATE-IP (Lightsail)
# ==========================================
function module_rotate_ip() {
    check_aws_credentials
    local CURRENT_IP=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -4 -s ifconfig.me")
    local AWS_REGION=$(ssh -o BatchMode=yes "$SSH_ALIAS" "curl -s -m 2 http://169.254.169.254/latest/meta-data/placement/region")
    
    if [ -z "$AWS_REGION" ]; then
        echo "❌ 无法获取实例所在地域 (Region)！"
        exit 1
    fi
    
    local INSTANCE_NAME=$(aws lightsail get-instances --region "$AWS_REGION" | jq -r ".instances[] | select(.publicIpAddress == \"$CURRENT_IP\") | .name")
    
    if [ -z "$INSTANCE_NAME" ]; then
        echo "❌ 无法通过 IP ($CURRENT_IP) 在您的 AWS 账户 (地域: $AWS_REGION) 中找到对应的 Lightsail 实例！请检查 AWS 认证或实例状态。"
        exit 1
    fi
    echo "🚨 开始对 AWS Lightsail 实例 [$INSTANCE_NAME] ($AWS_REGION) 执行断臂求生换 IP..."
    
    echo "1. 探测旧的闲置 Static IP..."
    local OLD_STATIC_IP_NAME=$(aws lightsail get-static-ips --region "$AWS_REGION" 2>/dev/null | jq -r ".staticIps[]? | select(.attachedTo == \"$INSTANCE_NAME\") | .name" 2>/dev/null || true)
    
    echo "2. 申请新的 Static IP..."
    local NEW_IP_NAME="ip-auto-$(date +%s)"
    aws lightsail allocate-static-ip --static-ip-name "$NEW_IP_NAME" --region "$AWS_REGION" > /dev/null
    local NEW_IP=$(aws lightsail get-static-ip --static-ip-name "$NEW_IP_NAME" --region "$AWS_REGION" | jq -r '.staticIp.ipAddress')
    echo "   -> 获得新 IP: $NEW_IP"
    
    echo "3. 强行绑定至实例..."
    aws lightsail attach-static-ip --static-ip-name "$NEW_IP_NAME" --instance-name "$INSTANCE_NAME" --region "$AWS_REGION" > /dev/null
    
    if [ -n "$OLD_STATIC_IP_NAME" ]; then
        echo "   -> 正在释放旧的 Static IP: $OLD_STATIC_IP_NAME (防止配额耗尽与额外扣费)..."
        aws lightsail release-static-ip --static-ip-name "$OLD_STATIC_IP_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true
    fi
    
    echo "4. 更新本地 SSH Config 以指向新 IP..."
    python3 -c "
import os, sys
config_path = os.path.expanduser('~/.ssh/config')
alias = '$SSH_ALIAS'
new_ip = '$NEW_IP'

lines = []
if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        lines = f.readlines()

new_lines = []
in_target_block = False
for line in lines:
    if line.strip().startswith('Host '):
        if line.strip() == f'Host {alias}':
            in_target_block = True
        else:
            in_target_block = False
    
    if in_target_block and line.strip().startswith('HostName '):
        new_lines.append(f'    HostName {new_ip}\n')
    else:
        new_lines.append(line)

with open(config_path, 'w') as f:
    f.writelines(new_lines)
"
    
    echo "5. 等待新 IP 的 SSH 端口恢复响应..."
    for i in {1..15}; do
        if ssh -o BatchMode=yes -o ConnectTimeout=2 "$SSH_ALIAS" "echo up" &>/dev/null; then
            echo "   ✅ SSH 已恢复连接！"
            break
        fi
        sleep 2
    done

    echo "6. 级联触发 Cloudflare DNS 更新与服务端节点配置同步..."
    module_rotate_dns
    
    echo "7. 自动触发远端 X-ray 指纹轮换以推送最新节点配置到 Telegram..."
    module_rotate_xray
    
    echo "🎉 自动换 IP 与自愈修复全部完成！"
}

function module_test() {
    if [ -z "$SSH_ALIAS" ]; then
        if [ "$CNSR_LANG" = "en" ]; then echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh test <alias> [tg])"; else echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh test <别名> [tg])"; fi
        exit 1
    fi
    
    echo "🧪 触发 Telegram 通知全套样张预览..."
    sync_node_scripts "$SSH_ALIAS" >/dev/null 2>&1 || true
    local REMOTE_HOME=$(ssh "$SSH_ALIAS" "eval echo ~\$USER")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"
    ssh -t "$SSH_ALIAS" "IS_TEST_MODE=1 bash ${DOCKER_APP_DIR}/reality_check.sh --test-tg"
}

function module_check() {
    echo "🩺 正在呼叫远端节点执行全面体检: $SSH_ALIAS"
    sync_node_scripts "$SSH_ALIAS"
    
    local REMOTE_HOME=$(ssh "$SSH_ALIAS" "eval echo ~\$USER")
    local DOCKER_APP_DIR="${REMOTE_HOME}/docker-apps/xray"
    
    ssh -t "$SSH_ALIAS" "bash ${DOCKER_APP_DIR}/reality_check.sh --notify"
    echo "✅ 远端体检完成！报告已通过 Telegram 下发。"
}

function module_update() {
    sync_node_scripts "$SSH_ALIAS"
    echo "🎉 节点 [$SSH_ALIAS] 全量组件与脱敏凭据热更新完成！"
}

# 路由分发
case "$COMMAND" in
    init)
        module_init "$@"
        ;;
    update)
        module_update "$@"
        ;;
    rotate)
        if [ "$SUBCOMMAND" = "sni" ]; then
            module_rotate_xray "$@"
        elif [ "$SUBCOMMAND" = "dns" ]; then
            module_rotate_dns "$@"
        elif [ "$SUBCOMMAND" = "ip" ]; then
            module_rotate_ip "$@"
        else
            if [ "$CNSR_LANG" = "en" ]; then
                echo "❌ Unknown rotation target. Usage: rotate <sni|dns|ip> <alias>"
            else
                echo "❌ 未知轮换目标。用法: rotate <sni|dns|ip> <alias>"
            fi
            exit 1
        fi
        ;;
    check)
        module_check "$@"
        ;;
    test)
        module_test "$@"
        ;;
    *)
        print_usage
        ;;
esac
