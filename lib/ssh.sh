#!/bin/bash
# Snack Connoisseur - SSH & Config Management Functions

SSH_CONFIG_PATH="${TEST_SSH_CONFIG:-$HOME/.ssh/config}"

function ensure_ssh_alias() {
    local alias="$1"
    local ip="$2"
    local user="$3"
    local port="${4:-22}"
    local identity_file="${5:-}"

    python3 -c "
import sys, os, re

config_path = os.path.expanduser('$SSH_CONFIG_PATH')
alias = sys.argv[1]
ip = sys.argv[2]
user = sys.argv[3]
port = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else '22'
identity_file = sys.argv[5] if len(sys.argv) > 5 else ''

home = os.path.expanduser('~')
if identity_file.startswith(home):
    identity_file = '~' + identity_file[len(home):]

os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
if not os.path.exists(config_path):
    with open(config_path, 'w') as f:
        pass
    try:
        os.chmod(config_path, 0o600)
    except Exception:
        pass

with open(config_path, 'r') as f:
    content = f.read()

block_pattern = r'(^Host\s+' + re.escape(alias) + r'\b(.*?))(?=\nHost\s|\Z)'
match = re.search(block_pattern, content, flags=re.DOTALL | re.MULTILINE)

if not match:
    lines = [f'Host {alias}', f'    HostName {ip}', f'    User {user}']
    if str(port) != '22' and str(port).strip():
        lines.append(f'    Port {port}')
    if identity_file:
        lines.append(f'    IdentityFile {identity_file}')
        if not re.search(r'^[ \t]*IdentitiesOnly\s+', content, flags=re.MULTILINE | re.IGNORECASE):
            lines.append('    IdentitiesOnly yes')
    if not re.search(r'^[ \t]*StrictHostKeyChecking\s+', content, flags=re.MULTILINE | re.IGNORECASE):
        lines.append('    StrictHostKeyChecking accept-new')

    new_block = '\n'.join(lines)
    if content.strip():
        new_content = content.rstrip() + '\n\n' + new_block + '\n'
    else:
        new_content = new_block + '\n'
else:
    block = match.group(1)
    if re.search(r'^[ \t]*HostName\s+', block, flags=re.MULTILINE):
        block = re.sub(r'(^[ \t]*HostName\s+)(\S+)', r'\g<1>' + ip, block, flags=re.MULTILINE)
    else:
        block = block.rstrip() + f'\n    HostName {ip}\n'

    if re.search(r'^[ \t]*User\s+', block, flags=re.MULTILINE):
        block = re.sub(r'(^[ \t]*User\s+)(\S+)', r'\g<1>' + user, block, flags=re.MULTILINE)
    else:
        block = block.rstrip() + f'\n    User {user}\n'

    if str(port) != '22' and str(port).strip():
        if re.search(r'^[ \t]*Port\s+', block, flags=re.MULTILINE):
            block = re.sub(r'(^[ \t]*Port\s+)(\d+)', r'\g<1>' + str(port), block, flags=re.MULTILINE)
        else:
            block = block.rstrip() + f'\n    Port {port}\n'
    else:
        block = re.sub(r'^[ \t]*Port\s+\d+\n?', '', block, flags=re.MULTILINE)

    if identity_file:
        if re.search(r'^[ \t]*IdentityFile\s+', block, flags=re.MULTILINE):
            block = re.sub(r'(^[ \t]*IdentityFile\s+)(\S+)', r'\g<1>' + identity_file, block, flags=re.MULTILINE)
        else:
            block = block.rstrip() + f'\n    IdentityFile {identity_file}\n'
        if not re.search(r'^[ \t]*IdentitiesOnly\s+', content, flags=re.MULTILINE | re.IGNORECASE):
            if not re.search(r'^[ \t]*IdentitiesOnly\s+', block, flags=re.MULTILINE | re.IGNORECASE):
                block = block.rstrip() + '\n    IdentitiesOnly yes\n'

    new_content = content[:match.start()] + block + content[match.end():]

with open(config_path, 'w') as f:
    f.write(new_content)
" "$alias" "$ip" "$user" "$port" "$identity_file"
}

function get_ssh_config() {
    local alias="$1"
    local field="${2:-HostName}"
    local field_lower=$(echo "$field" | tr '[:upper:]' '[:lower:]')

    python3 -c "
import sys, os, re
config_path = os.path.expanduser('$SSH_CONFIG_PATH')
alias = sys.argv[1]
field = sys.argv[2].lower()

if not os.path.exists(config_path):
    sys.exit(1)

with open(config_path, 'r') as f:
    content = f.read()

block_pattern = r'(^Host\s+' + re.escape(alias) + r'\b(.*?))(?=\nHost\s|\Z)'
match = re.search(block_pattern, content, flags=re.DOTALL | re.MULTILINE)
if match:
    block = match.group(1)
    for line in block.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == field:
            print(parts[1])
            sys.exit(0)
sys.exit(1)
" "$alias" "$field_lower" 2>/dev/null
}

function get_real_host() {
    local alias="${1:-$SSH_ALIAS}"
    local host=""
    if [ -n "$alias" ]; then
        host=$(get_ssh_config "$alias" "HostName")
    fi
    if [ -z "$host" ]; then
        if [ -f "$SSH_CONFIG_PATH" ]; then
            host=$(ssh -F "$SSH_CONFIG_PATH" -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
        else
            host=$(ssh -G "$alias" 2>/dev/null | awk '/^hostname / {print $2}')
        fi
    fi
    if [ -z "$host" ]; then
        host="$alias"
    fi
    echo "$host"
}

function check_ssh_conn() {
    local target="$1"
    local timeout="${2:-5}"
    local opts=(
        -o BatchMode=yes
        -o StrictHostKeyChecking=no
        -o ConnectTimeout="$timeout"
    )
    if [ -f "$SSH_CONFIG_PATH" ]; then
        opts+=(-F "$SSH_CONFIG_PATH")
    fi
    ssh "${opts[@]}" "$target" "echo alive" >/dev/null 2>&1
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

function detect_remote_ipv6() {
    local target="$1"
    local opts=(-o BatchMode=yes)
    if [ -f "$SSH_CONFIG_PATH" ]; then
        opts+=(-F "$SSH_CONFIG_PATH")
    fi
    ssh "${opts[@]}" "$target" "
        ip=\$(hostname -I 2>/dev/null | tr ' ' '\n' | grep ':' | grep -v '^fe80' | grep -v '^fc' | grep -v '^fd' | head -n1)
        if [ -z \"\$ip\" ]; then
            ip=\$(curl -6 -s --connect-timeout 3 ifconfig.me 2>/dev/null || true)
        fi
        echo \"\$ip\"
    " 2>/dev/null || true
}

function upgrade_ssh_config_hostname() {
    local alias="$1"
    local domain="$2"

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
import sys, os, re
config_path = os.path.expanduser('$SSH_CONFIG_PATH')
alias = sys.argv[1]
domain = sys.argv[2]
try:
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            content = f.read()
        pattern = r'(Host\s+' + re.escape(alias) + r'\s+[\s\S]*?HostName\s+)(\S+)'
        new_content = re.sub(pattern, r'\g<1>' + domain, content, count=1)
        with open(config_path, 'w') as f:
            f.write(new_content)
        print('🌐 已成功将 ~/.ssh/config 中的 HostName 升级为伪装域名:', domain)
except Exception as e:
    pass
" "$alias" "$domain" 2>/dev/null || true
    else
        echo "ℹ️ 新伪装域名 ($domain) 全网 DNS 广播扩散中，当前本地 SSH Config 维持 IP 连接以保障稳定。"
    fi
}
