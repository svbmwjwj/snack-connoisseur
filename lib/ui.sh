#!/bin/bash
# Snack Connoisseur - UI & Interactive Functions

function print_usage() {
    local exit_code="${1:-0}"
    local lang="${CNSR_LANG:-zh}"
    if [ "$lang" = "en" ]; then
        cat << 'EOF'
Snack Connoisseur (cnsr)

USAGE:
  ./cnsr.sh <command> [alias] [arguments] [options]

COMMANDS:
  init <alias> [ip]            Provision a new VPS node (Options: --harden, --debug)
  init-aws <alias>             Provision AWS Lightsail node (Options: --region, --count, --bundle, --blueprint)
  init-aws -f <conf>           Batch provision AWS nodes from a config file
  destroy-aws <alias|pattern>  Destroy AWS Lightsail instance(s) and clean SSH config (Option: --region)
  check <alias>                Comprehensive health check: latency, SNI quality, BBR, container
  update <alias>               Full hot-update: sync runner shim, self-healing probe, sanitized env
  rotate-sni <alias>           Force fingerprint rotation: reset UUID, keys, ShortID, and SNI
  rotate-dns <alias>           Active domain rotation: refresh secondary domain via Cloudflare
  rotate-ip <alias>            Ultimate survival: auto rebind new AWS Lightsail IP & cascade update
  test-tg <alias>              Push preview sample pack (5 alerts) to Telegram
  test-sni <alias>             Simulate SNI evaluation without modifying configuration
  lang [zh|en]                 Switch system language (Simplified Chinese zh / English en)
  help                         Show this help manual

OPTIONS:
  --harden                     Enable high-security hardening (for init)
  --debug                      Enable verbose debugging output (for init)
  --detach                     Run batch provisioning in background and detach immediately
  --region <region>            Specify AWS region (for init-aws, destroy-aws)
  --count <N>                  Specify node count for AWS initialization (Max: 20)
  --key-pair <name>            Specify AWS key pair name (for init-aws)
  -f, --file <conf>            Specify batch config file for init-aws
  -h, --help, -help            Show this help manual

EXAMPLES:
  ./cnsr.sh init sg_aws 198.51.100.1 --harden      # Compound action initialization
  ./cnsr.sh init-aws aws-node --region ap-northeast-1 --count 3  # Provision 3 AWS nodes
  ./cnsr.sh init-aws -f jp_aws-lightsail --count 3 # Batch provision from preset
  ./cnsr.sh init-aws -f jp_aws-lightsail --count 3 --detach # Detached batch deploy (safe to close terminal)
  ./cnsr.sh destroy-aws "jp_aws-lightsail-*" --region ap-northeast-1 # Batch destroy nodes
  ./cnsr.sh test-tg sg_aws                         # Preview Telegram alert samples
  ./cnsr.sh check sg_aws                           # Full node health check
  ./cnsr.sh test-sni sg_aws                        # Dry-run SNI rotation
  ./cnsr.sh lang en                                # Switch language to English
EOF
    else
        cat << 'EOF'
Snack Connoisseur (cnsr)

用法 / USAGE:
  ./cnsr.sh <命令> [别名] [IP] [选项]

核心命令 (COMMANDS):
  init <别名> [IP]             初始化新裸机 (选项: --harden 开启加固, --debug 开启调试)
  init-aws <别名>              初始化 AWS 实例 (选项: --region, --count, --bundle, --blueprint)
  init-aws -f <模版预设>        根据地区预设模版批量开机 (如 -f jp_aws-lightsail --count 3)
  destroy-aws <别名|通配符>    销毁 AWS 实例与静态 IP，清理本地 SSH 配置 (必须: --region)
  check <别名>                 全方位健康体检：延迟测评、SNI质量、内核与容器
  update <别名>                组件全量热更新：平滑同步最新垫片与脱敏凭据
  rotate-sni <别名>            强制指纹轮换：重置 UUID、X25519 密钥、ShortID 与 SNI
  rotate-dns <别名>            主动域变换：调用 Cloudflare API 刷新大厂内网级二级域名
  rotate-ip <别名>             终极断臂求生：AWS Lightsail 自动换绑静态 IP 并自愈
  test-tg <别名>               向 Telegram 推送全套 5 种通知样张（附带 ⚠️ 标签）
  test-sni <别名>              空转预演模式：只嗅探高分域名，不修改配置
  lang [zh|en]                 切换系统语言 (简体中文 zh / English en)
  help                         显示本帮助手册

参数选项 (OPTIONS):
  --harden                     (用于 init) 启用高强度系统加固与防爆破防火墙
  --debug                      (用于 init) 开启底层详细调试安装日志 (单台逐个同步执行)
  --detach                     (用于 init-aws) 启用脱机模式，转入后台运行并可安全退出终端
  --region <region>            (用于 init-aws, destroy-aws) 指定 AWS 区域
  --count <N>                  (用于 init-aws) 指定一次性部署的 AWS 节点数量 (最大上限: 20)
  --key-pair <名称>            (用于 init-aws) 指定 AWS 云端公钥名称
  -f, --file <模版预设>        (用于 init-aws) 指定批量节点模版文件 (如 jp_aws-lightsail)
  -h, --help                   显示本帮助手册

使用示例 (EXAMPLES):
  ./cnsr.sh init sg_aws 198.51.100.1 --harden      # 复合动作初始化并加固
  ./cnsr.sh init-aws aws-node --region ap-northeast-1 --count 3 # 一次性部署 3 台 AWS 节点
  ./cnsr.sh init-aws -f jp_aws-lightsail --count 3 # 从预设模版批量拉起 3 台日本节点
  ./cnsr.sh init-aws -f jp_aws-lightsail --count 3 --detach # 脱机批量拉起 (可直接关闭终端)
  ./cnsr.sh destroy-aws "jp_aws-lightsail-*" --region ap-northeast-1 # 批量清理实例与本地 SSH
  ./cnsr.sh test-tg sg_aws                         # 触发 Telegram 样张预览
  ./cnsr.sh check sg_aws                           # 全面体检节点质量
  ./cnsr.sh test-sni sg_aws                        # 仅评测高分域名
  ./cnsr.sh lang zh                                # 切换系统语言为中文
EOF
    fi
    exit "$exit_code"
}

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
        return 0
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

# 动态重绘进度条辅助函数 (Homebrew 风格)
function render_bar_line() {
    local current="$1"
    local total="$2"
    local suffix="${3:-}"
    local bar_width="${4:-32}"

    if [ "$total" -le 0 ]; then total=1; fi
    if [ "$current" -gt "$total" ]; then current="$total"; fi

    local percent=$((current * 100 / total))
    local filled=$((current * bar_width / total))
    local unfilled=$((bar_width - filled))

    local bar=""
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<unfilled; i++)); do bar+="░"; done

    printf "\r\033[2K    [\033[36m%s\033[0m] %d/%d (%d%%) %s" "$bar" "$current" "$total" "$percent" "$suffix"
}

# 进度条固化完成行
function render_bar_done() {
    local step="$1"
    local summary="$2"
    local total="$3"
    local duration="$4"
    printf "\r\033[2K\033[32m✔\033[0m [%s] %s (%d/%d) [耗时: %ds]\n" "$step" "$summary" "$total" "$total" "$duration"
}
