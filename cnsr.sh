#!/bin/bash
# Snack Connoisseur - 全栈模块化管控中枢 (cnsr)

set -e

# 加载环境变量
if [ -f ".env" ]; then
    source .env
elif [ -f ".env.example" ]; then
    echo "📄 正在从 .env.example 生成本地 .env 文件..."
    cp .env.example .env
    source .env
fi
export CNSR_LANG="${CNSR_LANG:-zh}"

# 导入 UI 库
source lib/ui.sh

# 处理可能是 -h 或 --help 的情况
if [ -z "$1" ] || [[ "$1" == "-h" || "$1" == "--help" || "$1" == "-help" || "$1" == "help" ]]; then
    print_usage 0
fi

COMMAND=$1
shift

if [[ "$COMMAND" == "lang" || "$COMMAND" == "set-lang" ]]; then
    TARGET_LANG="$1"
    if [ "$TARGET_LANG" != "zh" ] && [ "$TARGET_LANG" != "en" ]; then
        echo "❌ 错误: 请指定有效语言代码 (zh 或 en)。"
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
fi

ALIAS=$1
if [ -z "$ALIAS" ] && [[ "$COMMAND" != "lang" && "$COMMAND" != "set-lang" ]]; then
    if [ "$CNSR_LANG" = "en" ]; then 
        echo "❌ Error: Please specify node alias. (Usage: ./cnsr.sh <command> <alias> [args])"
    else 
        echo "❌ 错误: 请指定节点别名。 (用法: ./cnsr.sh <命令> <别名> [参数])"
    fi
    exit 1
fi
shift

case "$COMMAND" in
    init)
        source core/init.sh
        module_init "$ALIAS" "$@"
        ;;
    init-aws)
        source core/init_aws.sh "$ALIAS" "$@"
        ;;
    check)
        source core/check.sh
        module_check "$ALIAS" "$@"
        ;;
    update)
        source core/update.sh
        module_update "$ALIAS" "$@"
        ;;
    rotate-sni)
        source core/rotate.sh
        module_rotate_sni "$ALIAS" "$@"
        ;;
    rotate-dns)
        source core/rotate.sh
        module_rotate_dns "$ALIAS" "$@"
        ;;
    rotate-ip|rotate-aws-ip)
        source core/rotate.sh
        module_rotate_ip "$ALIAS" "$@"
        ;;
    test-tg)
        source core/test.sh
        module_test "$ALIAS" tg "$@"
        ;;
    test-sni)
        source core/rotate.sh
        module_rotate_sni "$ALIAS" --dry-run "$@"
        ;;
    *)
        if [ "$CNSR_LANG" = "en" ]; then
            echo "❌ Error: Unknown command: $COMMAND"
        else
            echo "❌ 错误: 未知命令: $COMMAND"
        fi
        exit 1
        ;;
esac
