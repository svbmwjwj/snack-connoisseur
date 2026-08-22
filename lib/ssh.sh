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

def get_host_blocks(full_content):
    header_matches = list(re.finditer(r'^[ \t]*Host[ \t]+([^\n]+)$', full_content, flags=re.MULTILINE))
    blocks = []
    for i, m in enumerate(header_matches):
        aliases = m.group(1).split()
        start = m.start()
        end = header_matches[i+1].start() if i + 1 < len(header_matches) else len(full_content)
        block_text = full_content[start:end]
        blocks.append({
            'aliases': aliases,
            'text': block_text,
            'start': start,
            'end': end
        })
    return blocks

def check_has_option(blocks, current_block_text, option_name):
    if re.search(r'^[ \t]*' + re.escape(option_name) + r'\s+', current_block_text, flags=re.MULTILINE | re.IGNORECASE):
        return True
    for b in blocks:
        if '*' in b['aliases']:
            if re.search(r'^[ \t]*' + re.escape(option_name) + r'\s+', b['text'], flags=re.MULTILINE | re.IGNORECASE):
                return True
    return False

blocks = get_host_blocks(content)
target_block = None
for b in blocks:
    if alias in b['aliases']:
        target_block = b
        break

if not target_block:
    lines = [f'Host {alias}', f'    HostName {ip}', f'    User {user}']
    if str(port) != '22' and str(port).strip():
        lines.append(f'    Port {port}')
    if identity_file:
        lines.append(f'    IdentityFile {identity_file}')
        if not check_has_option(blocks, '', 'IdentitiesOnly'):
            lines.append('    IdentitiesOnly yes')
    if not check_has_option(blocks, '', 'StrictHostKeyChecking'):
        lines.append('    StrictHostKeyChecking accept-new')

    new_block = '\n'.join(lines)
    if content.strip():
        new_content = content.rstrip() + '\n\n' + new_block + '\n'
    else:
        new_content = new_block + '\n'
else:
    block_text = target_block['text']
    if re.search(r'^[ \t]*HostName\s+', block_text, flags=re.MULTILINE):
        block_text = re.sub(r'(^[ \t]*HostName\s+)(\S+)', r'\g<1>' + ip, block_text, flags=re.MULTILINE)
    else:
        header_end = block_text.find('\n')
        if header_end != -1:
            block_text = block_text[:header_end+1] + f'    HostName {ip}\n' + block_text[header_end+1:]
        else:
            block_text = block_text + f'\n    HostName {ip}\n'

    if re.search(r'^[ \t]*User\s+', block_text, flags=re.MULTILINE):
        block_text = re.sub(r'(^[ \t]*User\s+)(\S+)', r'\g<1>' + user, block_text, flags=re.MULTILINE)
    else:
        header_end = block_text.find('\n')
        if header_end != -1:
            block_text = block_text[:header_end+1] + f'    User {user}\n' + block_text[header_end+1:]
        else:
            block_text = block_text + f'\n    User {user}\n'

    if str(port) != '22' and str(port).strip():
        if re.search(r'^[ \t]*Port\s+', block_text, flags=re.MULTILINE):
            block_text = re.sub(r'(^[ \t]*Port\s+)(\d+)', r'\g<1>' + str(port), block_text, flags=re.MULTILINE)
        else:
            block_text = block_text.rstrip() + f'\n    Port {port}\n'
    else:
        block_text = re.sub(r'^[ \t]*Port\s+\d+\n?', '', block_text, flags=re.MULTILINE)

    if identity_file:
        if re.search(r'^[ \t]*IdentityFile\s+', block_text, flags=re.MULTILINE):
            block_text = re.sub(r'(^[ \t]*IdentityFile\s+)(\S+)', r'\g<1>' + identity_file, block_text, flags=re.MULTILINE)
        else:
            block_text = block_text.rstrip() + f'\n    IdentityFile {identity_file}\n'
        if not check_has_option(blocks, block_text, 'IdentitiesOnly'):
            block_text = block_text.rstrip() + '\n    IdentitiesOnly yes\n'

    new_content = content[:target_block['start']] + block_text + content[target_block['end']:]

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

header_matches = list(re.finditer(r'^[ \t]*Host[ \t]+([^\n]+)$', content, flags=re.MULTILINE))
for i, m in enumerate(header_matches):
    aliases = m.group(1).split()
    if alias in aliases:
        start = m.start()
        end = header_matches[i+1].start() if i + 1 < len(header_matches) else len(content)
        block_text = content[start:end]
        for line in block_text.splitlines():
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
        header_matches = list(re.finditer(r'^[ \t]*Host[ \t]+([^\n]+)$', content, flags=re.MULTILINE))
        target_block = None
        for i, m in enumerate(header_matches):
            aliases = m.group(1).split()
            if alias in aliases:
                start = m.start()
                end = header_matches[i+1].start() if i + 1 < len(header_matches) else len(content)
                target_block = (start, end, content[start:end])
                break
        if target_block:
            start, end, block_text = target_block
            if re.search(r'^[ \t]*HostName\s+', block_text, flags=re.MULTILINE):
                block_text = re.sub(r'(^[ \t]*HostName\s+)(\S+)', r'\g<1>' + domain, block_text, flags=re.MULTILINE)
            else:
                header_end = block_text.find('\n')
                if header_end != -1:
                    block_text = block_text[:header_end+1] + f'    HostName {domain}\n' + block_text[header_end+1:]
                else:
                    block_text = block_text + f'\n    HostName {domain}\n'
            new_content = content[:start] + block_text + content[end:]
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

function remove_ssh_alias() {
    local pattern="$1"
    local cfg="${TEST_SSH_CONFIG:-${SSH_CONFIG_PATH:-$HOME/.ssh/config}}"
    python3 -c "
import sys, os, re, fnmatch
config_path = os.path.expanduser(sys.argv[1])
pattern = sys.argv[2]
if not os.path.exists(config_path):
    sys.exit(0)

with open(config_path, 'r') as f:
    content = f.read()

header_matches = list(re.finditer(r'^[ \t]*Host[ \t]+([^\n]+)$', content, flags=re.MULTILINE))
new_blocks = []

for i, m in enumerate(header_matches):
    aliases = m.group(1).split()
    start = m.start()
    end = header_matches[i+1].start() if i + 1 < len(header_matches) else len(content)
    block_text = content[start:end]
    
    match = False
    for a in aliases:
        if a == pattern or fnmatch.fnmatch(a, pattern):
            match = True
            break
    if not match:
        new_blocks.append(block_text)

prefix_text = ''
if header_matches:
    prefix_text = content[:header_matches[0].start()]

with open(config_path, 'w') as f:
    res_str = prefix_text + ''.join(new_blocks).strip()
    if res_str:
        f.write(res_str + '\n')
    else:
        f.write('')
" "$cfg" "$pattern"
}


