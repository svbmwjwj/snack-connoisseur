#!/bin/bash
# tg_templates.sh - Telegram 消息通知模板中心 (支持 CNSR_LANG=zh / en)
# 你可以根据个人喜好在此自由修改 Telegram 消息格式、Emoji、排版与文案

CNSR_LANG="${CNSR_LANG:-zh}"

TEST_PREFIX=""
if [ "$IS_TEST_MODE" = "1" ] || [ "$TEST_TG" = "true" ] || [ "$MOCK_BROKEN" = "true" ] || [ "$MOCK_SNI_PROBE_FAIL" = "1" ]; then
    if [ "$CNSR_LANG" = "en" ]; then
        TEST_PREFIX=$'⚠️ *[TEST MODE / Simulation]* ⚠️\n\n'
    else
        TEST_PREFIX=$'⚠️ *[TEST MODE / 模拟演练]* ⚠️\n\n'
    fi
fi

# 1. SNI 域名阻断告警 (触发自愈)
function tpl_sni_blocked() {
    local alias="$1"
    local current_sni="$2"
    local cidr_v4="$3"

    if [ "$CNSR_LANG" = "en" ]; then
        cat <<EOF
${TEST_PREFIX}🚨 *SNI Blocked Alert*

- *Node Alias*: \`${alias}\`
- *Blocked SNI*: \`${current_sni}\`
- *Target CIDR*: \`${cidr_v4}\`
- *Status*: 🔴 TLS Handshake Failed / Blocked

☁️ Triggering cloud cluster scan self-healing... Expected 1~2 mins. New configuration will be pushed automatically.
EOF
    else
        cat <<EOF
${TEST_PREFIX}🚨 *SNI 域名阻断告警*

- *节点别名*: \`${alias}\`
- *受阻 SNI*: \`${current_sni}\`
- *探测网段*: \`${cidr_v4}\`
- *当前状态*: 🔴 TLS 握手失败 / 已被阻断

☁️ 正在触发云端集群热扫描自愈... 预计耗时 1~2 分钟。自愈完成后将自动推送新配置。
EOF
    fi
}

# 2. X-ray REALITY 节点凭据推送 (初始部署完成 或 SNI/IP 更换成功)
function tpl_node_config() {
    local is_init="$1" # "true" 或 "false"
    local alias="$2"
    local host="$3"
    local sni="$4"
    local uuid="$5"
    local pubkey="$6"
    local shortid="$7"
    local qx_config="$8"
    local vless_uri="$9"

    if [ "$CNSR_LANG" = "en" ]; then
        local title="🚀 *X-ray Node Deployed*"
        if [ "$is_init" != "true" ]; then
            title="🔄 *X-ray Node Rotated*"
        fi

        cat <<EOF
${TEST_PREFIX}${title}

- *Node Alias*: \`${alias}\`
- *Domain*: \`${host}\`
- *Target SNI*: \`${sni}\`

- *Quantumult X Config*:
\`\`\`text
${qx_config}
\`\`\`

- *V2rayN / Clash Meta Config*:
\`\`\`text
${vless_uri}
\`\`\`
EOF
    else
        local title="🚀 *X-ray 节点部署完成*"
        if [ "$is_init" != "true" ]; then
            title="🔄 *X-ray 节点配置换新*"
        fi

        cat <<EOF
${TEST_PREFIX}${title}

- *节点别名*: \`${alias}\`
- *伪装域名*: \`${host}\`
- *目标 SNI*: \`${sni}\`

- *Quantumult X 配置*:
\`\`\`text
${qx_config}
\`\`\`

- *V2rayN / Clash Meta 配置*:
\`\`\`text
${vless_uri}
\`\`\`
EOF
    fi
}

# 3. 严重故障致命告警 (兜底机制)
function tpl_alert_failure() {
    local alias="$1"
    local failure_reason="$2"
    local impact="${3:-节点当前不可用}"
    local recommendation="${4:-请登录控制台检查或执行 \`./cnsr.sh check $alias\`}"

    if [ "$CNSR_LANG" = "en" ]; then
        impact="${3:-Node currently unreachable}"
        recommendation="${4:-Please check server console or run \`./cnsr.sh check $alias\`}"
        cat <<EOF
${TEST_PREFIX}🚨 *Critical Alert: Self-Healing Failed*

- *Node Alias*: \`${alias}\`
- *Failure Reason*: ${failure_reason}
- *Impact*: ${impact}
- *Recommendation*: ${recommendation}
EOF
    else
        cat <<EOF
${TEST_PREFIX}🚨 *致命故障告警: 节点自愈失败*

- *节点别名*: \`${alias}\`
- *失败原因*: ${failure_reason}
- *故障影响*: ${impact}
- *处理建议*: ${recommendation}
EOF
    fi
}

# 4. 节点体检 - 完整报表 (用于体检与自愈验收)
function tpl_health_full() {
    local alias="$1"
    local conclusion="$2"
    local ipv4="$3"
    local ipv6="$4"
    local host="$5"
    local domain_status="$6"
    local ping_report="$7"
    local sni="$8"
    local sni_status_code="$9"
    local sni_base="${10}"
    local sni_handshake="${11}"
    local sni_cert="${12}"
    local sni_cdn="${13}"
    local sni_rating="${14}"
    local sys_uptime="${15}"
    local tfo_status="${16}"
    local bbr_status="${17}"
    local udp_status="${18}"
    local container_status="${19}"
    local container_uptime="${20}"
    local flow="${21}"
    local uuid_masked="${22}"
    local pub_masked="${23}"
    local sid="${24}"

    local display_ipv6="$ipv6"
    if [ -z "$display_ipv6" ] || [ "$display_ipv6" = "N/A" ] || [ "$display_ipv6" = "none" ]; then
        if [ "$CNSR_LANG" = "en" ]; then
            display_ipv6="Unassigned (IPv4-Only)"
        else
            display_ipv6="未分配 (IPv4-Only)"
        fi
    fi

    if [ "$CNSR_LANG" = "en" ]; then
        cat <<EOF
${TEST_PREFIX}🩺 *X-ray Node Health Report*

- *Node Alias*: \`${alias}\`
- *Conclusion*: ${conclusion}

🌐 *Network & DNS*
- *IPv4*: \`${ipv4}\`
- *IPv6*: \`${display_ipv6}\`
- *Domain*: \`${host}\`
- *DNS Status*: ${domain_status}

📡 *Outbound Latency*
${ping_report}

🎯 *SNI Status*
- *Current SNI*: \`${sni}\`
- *HTTP Status*: ${sni_status_code}
- *Protocol*: ${sni_base}
- *Handshake*: ${sni_handshake}
- *Cert Status*: ${sni_cert}
- *CDN Detected*: ${sni_cdn}
- *Rating*: ${sni_rating}

💻 *System & Kernel*
- *Uptime*: ${sys_uptime}
- *TCP Fast Open*: ${tfo_status}
- *Congestion Control*: ${bbr_status}
- *UDP Forward*: ${udp_status}

🐳 *Container & Proxy*
- *Container*: ${container_status} (${container_uptime})
- *Flow*: \`${flow}\`
- *UUID*: \`${uuid_masked}\`
- *Public Key*: \`${pub_masked}\`
- *Short ID*: \`${sid}\`
EOF
    else
        cat <<EOF
${TEST_PREFIX}🩺 *X-ray 节点深度体检报告*

- *节点别名*: \`${alias}\`
- *体检结论*: ${conclusion}

🌐 *网络与 DNS 解析*
- *IPv4 地址*: \`${ipv4}\`
- *IPv6 地址*: \`${display_ipv6}\`
- *伪装域名*: \`${host}\`
- *解析状态*: ${domain_status}

📡 *出站网络延迟*
${ping_report}

🎯 *SNI 伪装域名状态*
- *当前 SNI*: \`${sni}\`
- *响应状态*: ${sni_status_code}
- *协议基线*: ${sni_base}
- *握手耗时*: ${sni_handshake}
- *证书状态*: ${sni_cert}
- *CDN 识别*: ${sni_cdn}
- *综合评分*: ${sni_rating}

💻 *系统与内核模块*
- *运行时间*: ${sys_uptime}
- *TCP Fast Open*: ${tfo_status}
- *拥塞控制*: ${bbr_status}
- *UDP 转发*: ${udp_status}

🐳 *容器与代理核心*
- *容器状态*: ${container_status} (${container_uptime})
- *流控算法*: \`${flow}\`
- *用户 UUID*: \`${uuid_masked}\`
- *Reality 公钥*: \`${pub_masked}\`
- *Short ID*: \`${sid}\`
EOF
    fi
}
