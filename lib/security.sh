#!/bin/bash
# Snack Connoisseur - Security & System Hardening Library

SSH_CONFIG_PATH="${TEST_SSH_CONFIG:-$HOME/.ssh/config}"

_SEC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$_SEC_LIB_DIR/ssh.sh" ]; then
    source "$_SEC_LIB_DIR/ssh.sh"
fi

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
        allowed_keys=("GATEWAY_URL" "GATEWAY_AUTH_KEY" "DEFAULT_CLOUD_USER" "CNSR_LANG" "BATCH_MODE")
    else
        allowed_keys=("TG_BOT_TOKEN" "TG_CHAT_ID" "GH_TOKEN" "DEFAULT_CLOUD_USER" "CNSR_LANG" "BATCH_MODE")
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

function module_harden_system() {
    local alias="$1"
    local ip="$2"
    local port="$3"
    local shadow_user="$4"

    if [ -z "$alias" ]; then
        echo "❌ 错误: module_harden_system 需要指定节点别名。"
        return 1
    fi

    # 解析目标 IP（如果未传入）
    if [ -z "$ip" ]; then
        if declare -f get_real_host >/dev/null 2>&1; then
            ip=$(get_real_host "$alias")
        elif declare -f get_ssh_config >/dev/null 2>&1; then
            ip=$(get_ssh_config "$alias" "HostName")
        fi
    fi
    if [ -z "$ip" ] || [ "$ip" = "$alias" ]; then
        ip=$(ssh -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
    fi
    if [ -z "$ip" ]; then
        ip="$alias"
    fi

    # 1. 影子管理员账号与高危混淆端口分配
    local scifi_names=("arrakis" "replicant" "skynet" "gotham" "trantor" "jedi" "cyberdyne" "matrix" "nostromo" "tesseract")
    if [ -z "$shadow_user" ]; then
        shadow_user=${scifi_names[$RANDOM % ${#scifi_names[@]}]}
    fi

    if [ -z "$port" ] || [ "$port" = "0" ] || [ "$port" = "22" ]; then
        port=$((RANDOM % 50000 + 10000))
    fi

    echo "⚙️ 正在执行系统加固及安全策略配置 (深层 Harden 模式)..."
    echo "   -> [加固] 分配高危混淆端口: $port"
    echo "   -> [加固] 创建影子管理员账号: $shadow_user"

    local ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        ssh_opts+=(-F "$SSH_CONFIG_PATH")
    fi

    # 2. 远端执行加固脚本（包含防呆、语法校验、自动回滚与幂等检测）
    ssh "${ssh_opts[@]}" "$alias" "
        set -e

        # 基础安全组件安装 (幂等)
        sudo apt-get update -y >/dev/null 2>&1 && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ufw fail2ban jq cron curl iputils-ping dnsutils >/dev/null 2>&1 || true

        # 影子管理员账号创建 (幂等)
        if ! id -u '$shadow_user' >/dev/null 2>&1; then
            sudo useradd -m -s /bin/bash '$shadow_user'
        fi

        # 免密 sudo 权限注入 (幂等)
        if [ ! -f '/etc/sudoers.d/90-$shadow_user' ] || ! grep -q '^$shadow_user ALL=(ALL) NOPASSWD:ALL' '/etc/sudoers.d/90-$shadow_user' 2>/dev/null; then
            echo '$shadow_user ALL=(ALL) NOPASSWD:ALL' | sudo tee '/etc/sudoers.d/90-$shadow_user' >/dev/null
            sudo chmod 440 '/etc/sudoers.d/90-$shadow_user'
        fi

        # SSH 认证密钥同步 (幂等)
        sudo mkdir -p '/home/$shadow_user/.ssh'
        if [ ! -s '/home/$shadow_user/.ssh/authorized_keys' ]; then
            if [ -f \"\$HOME/.ssh/authorized_keys\" ]; then
                sudo cp \"\$HOME/.ssh/authorized_keys\" '/home/$shadow_user/.ssh/authorized_keys'
            elif [ -f '/root/.ssh/authorized_keys' ]; then
                sudo cp '/root/.ssh/authorized_keys' '/home/$shadow_user/.ssh/authorized_keys'
            else
                sudo touch '/home/$shadow_user/.ssh/authorized_keys'
            fi
        fi
        sudo chown -R '$shadow_user:$shadow_user' '/home/$shadow_user/.ssh'
        sudo chmod 700 '/home/$shadow_user/.ssh'
        sudo chmod 600 '/home/$shadow_user/.ssh/authorized_keys'

        # UFW 防火墙加固配置 (幂等，先临时放行 22 避免切断连接)
        sudo ufw default deny incoming >/dev/null 2>&1 || true
        sudo ufw default allow outgoing >/dev/null 2>&1 || true
        sudo ufw allow 22/tcp >/dev/null 2>&1 || true
        sudo ufw allow '$port'/tcp >/dev/null 2>&1 || true
        sudo ufw allow 443/tcp >/dev/null 2>&1 || true
        sudo ufw --force enable >/dev/null 2>&1 || true
        sudo systemctl enable --now fail2ban >/dev/null 2>&1 || true

        # sshd_config 安全加固与幂等注入
        sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.cnsr_bak

        # 注入/更新 Port
        if grep -qE '^[#[:space:]]*Port[[:space:]]+' /etc/ssh/sshd_config; then
            sudo sed -i -E 's/^[#[:space:]]*Port[[:space:]]+.*/Port '"$port"'/' /etc/ssh/sshd_config
        else
            echo "Port $port" | sudo tee -a /etc/ssh/sshd_config >/dev/null
        fi

        # 注入/更新 PermitRootLogin
        if grep -qE '^[#[:space:]]*PermitRootLogin[[:space:]]+' /etc/ssh/sshd_config; then
            sudo sed -i -E 's/^[#[:space:]]*PermitRootLogin[[:space:]]+.*/PermitRootLogin no/' /etc/ssh/sshd_config
        else
            echo "PermitRootLogin no" | sudo tee -a /etc/ssh/sshd_config >/dev/null
        fi

        # 注入/更新 PasswordAuthentication
        if grep -qE '^[#[:space:]]*PasswordAuthentication[[:space:]]+' /etc/ssh/sshd_config; then
            sudo sed -i -E 's/^[#[:space:]]*PasswordAuthentication[[:space:]]+.*/PasswordAuthentication no/' /etc/ssh/sshd_config
        else
            echo "PasswordAuthentication no" | sudo tee -a /etc/ssh/sshd_config >/dev/null
        fi

        # 注入/更新 AllowUsers (防重复添加)
        if grep -qE '^[#[:space:]]*AllowUsers[[:space:]]+' /etc/ssh/sshd_config; then
            if ! grep -E '^[[:space:]]*AllowUsers[[:space:]]+' /etc/ssh/sshd_config | grep -qw "$shadow_user"; then
                sudo sed -i -E 's/^[#[:space:]]*AllowUsers[[:space:]]+.*/AllowUsers '"$shadow_user"'/' /etc/ssh/sshd_config
            fi
        else
            echo "AllowUsers $shadow_user" | sudo tee -a /etc/ssh/sshd_config >/dev/null
        fi

        # 执行 sshd 语法校验 (防锁死防呆保护)
        if sudo sshd -t >/dev/null 2>&1; then
            sudo rm -f /etc/ssh/sshd_config.cnsr_bak
        else
            echo '❌ 严重错误: sshd_config 语法校验失败，已自动回滚配置！' >&2
            sudo cp /etc/ssh/sshd_config.cnsr_bak /etc/ssh/sshd_config
            sudo rm -f /etc/ssh/sshd_config.cnsr_bak
            exit 1
        fi

        # 迁移既有 Docker 应用目录 (如果存在且当前非影子用户)
        CURRENT_USER=\$(id -un)
        OLD_DOCKER_APP_DIR=\"/home/\$CURRENT_USER/docker-apps/xray\"
        NEW_DOCKER_APP_DIR=\"/home/$shadow_user/docker-apps/xray\"

        if [ \"\$CURRENT_USER\" != '$shadow_user' ]; then
            sudo mkdir -p \"/home/$shadow_user/docker-apps\"
            if [ -d \"\$OLD_DOCKER_APP_DIR\" ] && [ ! -d \"\$NEW_DOCKER_APP_DIR\" ]; then
                sudo mv \"\$OLD_DOCKER_APP_DIR\" \"/home/$shadow_user/docker-apps/\"
            elif [ -d \"\$OLD_DOCKER_APP_DIR\" ] && [ -d \"\$NEW_DOCKER_APP_DIR\" ] && [ \"\$OLD_DOCKER_APP_DIR\" != \"\$NEW_DOCKER_APP_DIR\" ]; then
                sudo cp -rn \"\$OLD_DOCKER_APP_DIR\"/* \"\$NEW_DOCKER_APP_DIR\"/ 2>/dev/null || true
            fi
            if [ -d \"/home/$shadow_user/docker-apps\" ]; then
                sudo chown -R '$shadow_user:$shadow_user' \"/home/$shadow_user/docker-apps\"
                for s in /home/'$shadow_user'/docker-apps/xray/*.sh; do
                    if [ -f \"\$s\" ]; then
                        sudo sed -i \"s|\$OLD_DOCKER_APP_DIR|\$NEW_DOCKER_APP_DIR|g\" \"\$s\" 2>/dev/null || true
                    fi
                done
            fi
        fi

        # 重启 sshd 服务
        sudo systemctl restart sshd 2>/dev/null || sudo systemctl restart ssh 2>/dev/null || sudo service ssh restart 2>/dev/null || true

        # 撤销 UFW 临时 22 端口放行
        if [ '$port' != '22' ]; then
            sudo ufw delete allow 22/tcp >/dev/null 2>&1 || true
        fi
    "
    local remote_status=$?
    if [ $remote_status -ne 0 ]; then
        echo "❌ 远端加固流程执行失败 (Exit Code: $remote_status)"
        return $remote_status
    fi

    echo "   -> [加固] 本地环境修正，适配新用户与端口..."
    if declare -f ensure_ssh_alias >/dev/null 2>&1; then
        ensure_ssh_alias "$alias" "$ip" "$shadow_user" "$port"
    else
        # 独立兜底 Python 3 写入 SSH Config
        python3 -c "
import sys, os, re
config_path = os.path.expanduser('$SSH_CONFIG_PATH')
alias, ip, user, port = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        content = f.read()
    block_pattern = r'(Host\s+' + re.escape(alias) + r'\b(.*?))(?=\nHost\s|\Z)'
    match = re.search(block_pattern, content, flags=re.DOTALL)
    if match:
        block = match.group(1)
        block = re.sub(r'(^[ \t]*User\s+)(\S+)', r'\g<1>' + user, block, flags=re.MULTILINE)
        if str(port) != '22':
            if re.search(r'^[ \t]*Port\s+', block, flags=re.MULTILINE):
                block = re.sub(r'(^[ \t]*Port\s+)(\d+)', r'\g<1>' + port, block, flags=re.MULTILINE)
            else:
                block = block.rstrip() + f'\n    Port {port}\n'
        else:
            block = re.sub(r'^[ \t]*Port\s+\d+\n?', '', block, flags=re.MULTILINE)
        new_content = content[:match.start()] + block + content[match.end():]
        with open(config_path, 'w') as f:
            f.write(new_content)
" "$alias" "$ip" "$shadow_user" "$port" 2>/dev/null || true
    fi

    # 3. AWS Lightsail 云防火墙放行 (若处于 AWS 环境且安装有 AWS CLI)
    if command -v aws >/dev/null 2>&1; then
        echo "   -> [加固] 尝试通过 AWS CLI 为 Lightsail 实例放行云端防火墙端口 $port..."
        local aws_region_probe
        aws_region_probe=$(ssh "${ssh_opts[@]}" "$alias" "curl -s -m 2 http://169.254.169.254/latest/meta-data/placement/region" 2>/dev/null || true)
        if [ -n "$aws_region_probe" ]; then
            local instance_name_probe
            instance_name_probe=$(aws lightsail get-instances --region "$aws_region_probe" 2>/dev/null | jq -r ".instances[] | select(.publicIpAddress == \"$ip\") | .name" 2>/dev/null || true)
            if [ -n "$instance_name_probe" ]; then
                aws lightsail open-instance-port --instance-name "$instance_name_probe" --region "$aws_region_probe" --port-info fromPort="$port",toPort="$port",protocol=TCP >/dev/null 2>&1 || true
                echo "   ✅ 已自动调用 AWS API 在 Lightsail 云防火墙中放行 $port 端口。"
            fi
        fi
    fi

    echo "✅ 深度安全加固完成！新身份已隐匿潜伏。"
    return 0
}
