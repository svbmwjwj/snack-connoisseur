#!/bin/bash
# runner.sh - 安全垫片引导器
set -e
DOCKER_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DOCKER_DIR"

TARGET_IP="PLACEHOLDER_IP"
SERVER_HOST="PLACEHOLDER_HOST"
SSH_ALIAS="PLACEHOLDER_ALIAS"
TARGET_CIDR_V4="PLACEHOLDER_CIDR_V4"
TARGET_CIDR_V6="PLACEHOLDER_CIDR_V6"

if [ -f "$DOCKER_DIR/.env" ]; then
    set -a
    source "$DOCKER_DIR/.env"
    set +a
fi

# 1. 尝试从网关或 GitHub 下载最新 reality_rotate.template.sh
UPDATED=false
if [ -n "$GATEWAY_URL" ]; then
    curl -sL -H "Authorization: Bearer $GATEWAY_AUTH_KEY" \
         "$GATEWAY_URL/api/raw/reality_rotate.template.sh" -o reality_rotate.sh.next 2>/dev/null || true
elif [ -n "$GH_TOKEN" ]; then
    curl -sL -H "Authorization: Bearer $GH_TOKEN" \
         "https://raw.githubusercontent.com/svbmwjwj/snack-connoisseur/main/templates/reality_rotate.template.sh" -o reality_rotate.sh.next 2>/dev/null || true
fi

# 2. 如果下载成功，进行变量替换并做语法校验
if [ -s reality_rotate.sh.next ]; then
    sed -i.bak -e "s|PLACEHOLDER_IP|$TARGET_IP|g" \
               -e "s|PLACEHOLDER_HOST|$SERVER_HOST|g" \
               -e "s|PLACEHOLDER_ALIAS|$SSH_ALIAS|g" \
               -e "s|PLACEHOLDER_CIDR_V4|$TARGET_CIDR_V4|g" \
               -e "s|PLACEHOLDER_CIDR_V6|$TARGET_CIDR_V6|g" \
               -e "s|/home/admin/docker-apps/xray|$DOCKER_DIR|g" reality_rotate.sh.next 2>/dev/null || \
    sed -i "s|PLACEHOLDER_IP|$TARGET_IP|g" reality_rotate.sh.next 2>/dev/null || true
    rm -f reality_rotate.sh.next.bak

    if bash -n reality_rotate.sh.next 2>/dev/null; then
        mv -f reality_rotate.sh.next reality_rotate.sh
        chmod +x reality_rotate.sh
        UPDATED=true
    else
        rm -f reality_rotate.sh.next
    fi
fi

# 3. 启动执行
if [ -x "$DOCKER_DIR/reality_rotate.sh" ]; then
    exec "$DOCKER_DIR/reality_rotate.sh" "$@"
fi
