#!/bin/bash
# Snack Connoisseur - AWS Cloud Initialization Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ui.sh" ]; then source "$LIB_DIR/ui.sh"; fi
if [ -f "$LIB_DIR/ssh.sh" ]; then source "$LIB_DIR/ssh.sh"; fi

SSH_ALIAS="$1"
shift

REGION=""
COUNT=1
EXTRA_INIT_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --region|-region)
            if [ $# -ge 2 ]; then
                REGION="$2"
                shift 2
            else
                shift
            fi
            ;;
        --count|-count)
            if [ $# -ge 2 ]; then
                COUNT="$2"
                shift 2
            else
                shift
            fi
            ;;
        --harden|-harden)
            EXTRA_INIT_ARGS+=("--harden")
            shift
            ;;
        --debug|-debug)
            EXTRA_INIT_ARGS+=("--debug")
            shift
            ;;
        *)
            shift
            ;;
    esac
done

if [ -z "$SSH_ALIAS" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh init-aws <alias> --region <region>)"
    else
        echo "❌ 错误: 请指定节点基础别名。 (用法: ./cnsr.sh init-aws <别名> --region <区域>)"
    fi
    exit 1
fi

if [ -z "$REGION" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: AWS mode requires --region. (e.g., --region ap-northeast-1)"
    else
        echo "❌ 错误: AWS 模式需要指定 --region。 (例如: --region ap-northeast-1)"
    fi
    exit 1
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

# 2. 防重检查: 遍历 count，检测 ~/.ssh/config 冲突
for ((i=1; i<=COUNT; i++)); do
    current_alias="${SSH_ALIAS}"
    if [ "$COUNT" -gt 1 ]; then
        current_alias="${SSH_ALIAS}-${i}"
    fi
    if grep -q -E "^Host $current_alias$" ~/.ssh/config 2>/dev/null; then
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Alias $current_alias already exists in ~/.ssh/config. Please clean up to avoid conflicts."
        else
            echo "❌ 错误: ~/.ssh/config 中已存在别名 $current_alias，请清理或更换别名防止冲突。"
        fi
        exit 1
    fi
done

# 3. 循环调用 uv run providers/aws.py create 拿 IP (并行执行)
for ((i=1; i<=COUNT; i++)); do
    (
        current_alias="${SSH_ALIAS}"
        if [ "$COUNT" -gt 1 ]; then
            current_alias="${SSH_ALIAS}-${i}"
        fi
        
        if [ "$CNSR_LANG" = "en" ]; then
            echo "☁️ Calling AWS API to create instance [$current_alias] in $REGION ..."
        else
            echo "☁️ 正在通过 AWS API 创建实例 [$current_alias] (区域: $REGION)..."
        fi
        
        # 临时禁用 strict 模式以避免 JSON 解析失败时直接退出
        set +e
        output=$(uv run providers/aws.py create --alias "$current_alias" --region "$REGION" --count 1 2>&1)
        status=$?
        set -e
        
        if [ $status -ne 0 ]; then
            echo "❌ 错误: AWS 实例创建失败 (AWS instance creation failed) [$current_alias]"
            echo "$output"
            exit 1
        fi
        
        # Parse JSON directly in Python to extract IP
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
except Exception as e:
    pass
")
        
        if [ -z "$ip" ]; then
            echo "❌ 错误: 未能获取到新实例的 IP (Failed to get IP for new instance) [$current_alias]"
            echo "Output was: $output"
            exit 1
        fi
        
        if [ "$CNSR_LANG" = "en" ]; then
            echo "✅ Instance [$current_alias] created! IP: $ip. Waiting 30s for sshd to start..."
        else
            echo "✅ 实例 [$current_alias] 创建成功，IP: $ip，等待云端 SSH 启动 (30秒)..."
        fi
        sleep 30
        
        # 4. 获取 IP 后内部调用 source core/init.sh 走部署
        echo "🚀 开始将部署移交至 core/init.sh... [$current_alias]"
        
        if [ -f "$SCRIPT_DIR/init.sh" ]; then
            source "$SCRIPT_DIR/init.sh"
            SSH_ALIAS="$current_alias"
            TARGET_IP="$ip"
            module_init "${EXTRA_INIT_ARGS[@]}"
        else
            echo "❌ 错误: 找不到 core/init.sh (core/init.sh not found)"
            exit 1
        fi
    ) &
done

wait
echo "✅ 所有实例编排完成 (All AWS instances provisioned and initialized)."
exit 0
