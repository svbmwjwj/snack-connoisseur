#!/bin/bash
# Snack Connoisseur - Get Links Module
# Fetches vless and qx configurations from remote nodes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

if [ -f "$LIB_DIR/ssh.sh" ]; then
    source "$LIB_DIR/ssh.sh"
fi

function module_link() {
    local alias_pattern="$1"
    
    if [ -z "$alias_pattern" ]; then
        echo "❌ 错误: 请指定节点别名或匹配模式 (例如: jp_aws-lightsail-1 或 jp_aws-lightsail-*)"
        return 1
    fi

    local target_aliases=()
    
    # 解析别名 (支持通配符模式，例如 jp_aws-lightsail-*)
    if [[ "$alias_pattern" == *"*"* ]]; then
        local base_pattern="${alias_pattern%\*}"
        local found=false
        while IFS= read -r line; do
            if [[ "$line" == Host\ $base_pattern* ]]; then
                local found_alias=$(echo "$line" | awk '{print $2}')
                target_aliases+=("$found_alias")
                found=true
            fi
        done < <(grep -i "^Host " ~/.ssh/config 2>/dev/null || true)
        
        if [ "$found" = false ]; then
            echo "❌ 未在 ~/.ssh/config 中找到匹配 '$alias_pattern' 的节点。"
            return 1
        fi
        
        # 排序别名以便整齐输出 (例如 jp_aws-lightsail-1, 2, 3...)
        IFS=$'\n' target_aliases=($(sort -V <<<"${target_aliases[*]}"))
        unset IFS
    else
        target_aliases=("$alias_pattern")
    fi

    local count=${#target_aliases[@]}
    echo "📡 正在从 $count 个节点提取订阅配置..."
    
    local all_vless=()
    local all_qx=()
    
    local BATCH_TMP_DIR=$(mktemp -d)

    # 并发抓取
    for cur_alias in "${target_aliases[@]}"; do
        (
            local remote_home=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$cur_alias" "eval echo ~\$USER" 2>/dev/null || echo "/home/admin")
            local docker_dir="${remote_home}/docker-apps/xray"
            local vless_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$cur_alias" "cat ${docker_dir}/vless.txt" 2>/dev/null || true)
            local qx_content=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$cur_alias" "cat ${docker_dir}/qx.txt" 2>/dev/null || true)
            
            if [ -n "$vless_content" ]; then
                echo "$vless_content" > "${BATCH_TMP_DIR}/${cur_alias}.vless"
            fi
            if [ -n "$qx_content" ]; then
                echo "$qx_content" > "${BATCH_TMP_DIR}/${cur_alias}.qx"
            fi
        ) &
    done
    
    # 简单的进度展示
    local total=$count
    while true; do
        local current_done=$(ls -1 "${BATCH_TMP_DIR}"/*.vless 2>/dev/null | wc -l | xargs)
        render_bar_line "$current_done" "$total" "提取配置中..." 32
        if [ "$current_done" -ge "$total" ]; then
            break
        fi
        # 若总数不一致且无新进展，设定一个超时，暂且等待所有进程退出
        if ! jobs %1 >/dev/null 2>&1; then 
            break
        fi
        sleep 0.15
    done
    wait
    
    local final_done=$(ls -1 "${BATCH_TMP_DIR}"/*.vless 2>/dev/null | wc -l | xargs)
    render_bar_done "✔" "节点配置提取完成" "$final_done" "0"
    echo ""

    if [ "$final_done" -eq 0 ]; then
        echo "❌ 未能从任何节点提取到配置文件 (请确保节点在线且已初始化)。"
        rm -rf "$BATCH_TMP_DIR"
        return 1
    fi

    # 聚合输出
    echo "==========================================================="
    echo "🔗 VLESS 订阅链接聚合 (共 $final_done 条) - 适用于 V2rayN / v2rayA"
    echo "==========================================================="
    for cur_alias in "${target_aliases[@]}"; do
        if [ -f "${BATCH_TMP_DIR}/${cur_alias}.vless" ]; then
            cat "${BATCH_TMP_DIR}/${cur_alias}.vless"
        fi
    done
    echo ""
    echo "==========================================================="
    echo "🔗 Quantumult X 节点配置聚合 (共 $final_done 条)"
    echo "==========================================================="
    for cur_alias in "${target_aliases[@]}"; do
        if [ -f "${BATCH_TMP_DIR}/${cur_alias}.qx" ]; then
            cat "${BATCH_TMP_DIR}/${cur_alias}.qx"
        fi
    done
    echo "==========================================================="
    echo "💡 提示: 您可以直接复制上述文本块，前往代理客户端中进行「从剪贴板批量导入」。"

    rm -rf "$BATCH_TMP_DIR"
}
