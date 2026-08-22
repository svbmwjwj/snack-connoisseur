#!/bin/bash
# Snack Connoisseur - AWS Cloud Destruction Module
# Part of core/ operations suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ui.sh" ]; then source "$LIB_DIR/ui.sh"; fi
if [ -f "$LIB_DIR/ssh.sh" ]; then source "$LIB_DIR/ssh.sh"; fi

SSH_ALIAS=""
REGION=""
PROFILE_INPUT=""

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
        -f|--profile|-profile)
            if [ $# -ge 2 ]; then
                PROFILE_INPUT="$2"
                shift 2
            else
                shift
            fi
            ;;
        *)
            if [ -z "$SSH_ALIAS" ] && [[ "$1" != -* ]]; then
                SSH_ALIAS="$1"
            fi
            shift
            ;;
    esac
done

if [ -n "$PROFILE_INPUT" ]; then
    RESOLVED_CONF=""
    if [ -f "$PROFILE_INPUT" ]; then
        RESOLVED_CONF="$PROFILE_INPUT"
    elif [ -f "templates/${PROFILE_INPUT}" ]; then
        RESOLVED_CONF="templates/${PROFILE_INPUT}"
    elif [ -f "templates/${PROFILE_INPUT}.conf" ]; then
        RESOLVED_CONF="templates/${PROFILE_INPUT}.conf"
    fi
    if [ -n "$RESOLVED_CONF" ] && [ -f "$RESOLVED_CONF" ]; then
        T_REGION=$(grep -E "^[[:space:]]*REGION=" "$RESOLVED_CONF" | head -n1 | cut -d= -f2- | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d '\r')
        T_ALIAS=$(grep -E "^[[:space:]]*ALIAS=" "$RESOLVED_CONF" | head -n1 | cut -d= -f2- | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d '\r')
        [ -z "$REGION" ] && REGION="$T_REGION"
        [ -z "$SSH_ALIAS" ] && SSH_ALIAS="${T_ALIAS}-*"
    fi
fi

if [ -z "$SSH_ALIAS" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: Please specify node alias or pattern to destroy. (Usage: ./cnsr.sh destroy-aws <alias_or_pattern> --region <region> OR ./cnsr.sh destroy-aws -f <profile>)"
    else
        echo "❌ 错误: 请指定要销毁的节点别名或匹配通配符。 (用法: ./cnsr.sh destroy-aws <别名或通配符> --region <区域> 或 ./cnsr.sh destroy-aws -f <模版>)"
    fi
    exit 1
fi

if [ -z "$REGION" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: destroy-aws requires --region or -f <profile>."
    else
        echo "❌ 错误: destroy-aws 必须指定 --region 或 -f <模版>。"
    fi
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "❌ Error: uv not found. Please install uv first."
    else
        echo "❌ 错误: 未找到 uv。请先安装 uv。"
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
set -a
source .env
set +a

if [ "$CNSR_LANG" = "en" ]; then
    echo "🗑️ Destroying AWS Lightsail instances matching [$SSH_ALIAS] in $REGION..."
else
    echo "🗑️ 正在销毁区域 [$REGION] 中匹配 [$SSH_ALIAS] 的 AWS Lightsail 实例..."
fi

set +e
output=$(uv run providers/aws.py delete --alias "$SSH_ALIAS" --region "$REGION" 2>&1)
status=$?
set -e

if [ $status -ne 0 ]; then
    echo "❌ 错误: 销毁操作执行失败 (Destruction failed):"
    echo "$output"
    exit 1
fi

# Clean up matching ~/.ssh/config entries
remove_ssh_alias "$SSH_ALIAS"

# Parse output
deleted_count=$(echo "$output" | uv run python3 -c "
import sys, json
try:
    for line in sys.stdin:
        line = line.strip()
        if line.startswith('{'):
            data = json.loads(line)
            print(data.get('count', 0))
            break
except Exception:
    print(0)
")

if [ "$deleted_count" -gt 0 ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        echo "✅ Successfully destroyed $deleted_count instance(s) and cleaned SSH config entries."
    else
        echo "✅ 成功销毁 $deleted_count 个实例并清理关联静态 IP 与本地 SSH 配置。"
    fi
else
    if [ "$CNSR_LANG" = "en" ]; then
        echo "ℹ️ No instances found matching '$SSH_ALIAS' in region $REGION."
    else
        echo "ℹ️ 在区域 $REGION 未找到匹配 '$SSH_ALIAS' 的实例。"
    fi
fi

exit 0
