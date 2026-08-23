# 🤖 Telegram 机器人消息模板配置与案例中心 (tg_templates.md)

本文件是项目中 Telegram 通知消息的 **Markdown 格式模版与案例定义**。
你可以直接在此文件中修改你喜欢的文案、排版、Emoji 或中英文语言风格。

---

## 🛠️ 如何使用与自定义

1. **直接编辑本文件**：在下方的 [模板编辑区](#-模板编辑区-editable-templates) 中修改对应的消息文本。
2. **保留动态变量**：每个模板都有对应的动态参数（例如 `${alias}`、`${sni}` 等），调整排版时请保留您需要展示的变量占位符。
3. **通知 AI 同步**：修改保存后，直接在对话中发送 **“请根据 tg_templates.md 更新机器人模板”**，AI 将自动将其编译同步至 `templates/tg_templates.sh` 并完成语法校验。

---

## 🏷️ 可用变量速查表 (Variables Reference)

| 变量名 | 含义说明 | 适用模板 | 示例值 |
| :--- | :--- | :--- | :--- |
| `${TEST_PREFIX}` | 模拟演练/测试模式提示前缀 (系统自动注入) | 全部模板 | `#### ⚠️ [TEST MODE / 模拟演练] ⚠️\n\n` |
| `${alias}` | 节点别名 / 服务器标识 | 全部模板 | `us_racknerd` |
| `${ip}` / `${ipv4}` | 服务器 IPv4 地址 | 2, 7, 8 | `192.129.143.41` |
| `${ipv6}` | 服务器 IPv6 地址 | 8 | `2607:f130:0:1::1` / `None` |
| `${host}` | 节点伪装二级域名 | 5, 8 | `metrics-api-v12.example.com` |
| `${sni}` / `${current_sni}` | 节点当前伪装的目标 SNI 域名 | 4, 5, 6, 8 | `gateway.icloud.com` |
| `${cidr_v4}` | 服务器所在的 IPv4 网段 (用于云端重扫) | 4 | `192.129.143.0/24` |
| `${title}` | 节点凭据推送标题 (区分初始化/轮换) | 5 | `🚀 *X-ray Node Deployed*` / `🔄 *X-ray Node Rotated*` |
| `${uuid}` / `${uuid_masked}` | X-ray 用户 UUID / 脱敏 UUID | 5, 8 | `d3b07384-d113-4632-9c93-2997707fa148` |
| `${pubkey}` / `${pub_masked}`| Reality 公钥 / 脱敏公钥 | 5, 8 | `5xO...masked` |
| `${shortid}` / `${sid}` | Reality ShortId | 5, 8 | `a1b2c3d4` |
| `${qx_config}` | Quantumult X 完整节点订阅行 | 5 | `vless=metrics...` |
| `${vless_uri}` | V2rayN / Clash Meta 标准 VLESS 链接 | 5 | `vless://uuid@host:443?...` |
| `${conclusion}` | 诊断综合评级结论 | 6, 7, 8 | `🟢 EXCELLENT` / `🔴 CRITICAL` |
| `${domain_status}` | 域名 DNS 解析状态 | 8 | `🟢 MATCH` / `🔴 MISMATCH` |
| `${ping_report}` | 国内外主流节点 Ping 延迟多行报告 | 8 | 见下方案例 |
| `${sni_status_code}` | SNI 目标网站 HTTP 状态码 | 8 | `200 OK` / `403 Forbidden` |
| `${sni_base}` | SNI 协议基线 (TLS 1.3 / H2) | 8 | `TLS 1.3 / H2` |
| `${sni_handshake}` | TLS 握手耗时 | 8 | `42ms` |
| `${sni_cert}` | 证书有效性及剩余天数 | 8 | `VALID (68 days)` |
| `${sni_cdn}` | 是否挂载已知 CDN | 8 | `Direct (No CDN)` / `Cloudflare` |
| `${sni_rating}` | SNI 综合评级评分 | 8 | `EXCELLENT (100/100)` |
| `${sys_uptime}` | 服务器系统运行时间 | 8 | `12 days, 4 hours` |
| `${tfo_status}` | TCP Fast Open 开启状态 | 8 | `🟢 ENABLED (3)` |
| `${bbr_status}` | TCP BBR 拥塞控制算法状态 | 8 | `🟢 bbr` |
| `${udp_status}` | UDP / FullCone 状态 | 8 | `🟢 OPEN` |
| `${container_status}` | X-ray Docker 容器状态 | 8 | `🟢 RUNNING` |
| `${container_uptime}` | 容器运行时长 | 8 | `Up 2 days` |
| `${flow}` | X-ray 流控算法 | 8 | `xtls-rprx-vision` |

---

## 📝 模板编辑区 (Editable Templates)

> 💡 **提示**：编辑下方代码块内的内容即可。系统使用 Telegram Markdown 语法（`*粗体*`、`\`行内代码\``、`\`\`\`代码块\`\`\``）。

### 模板 1：阶段一 - 环境部署完成 (`tpl_stage1`)
```markdown
${TEST_PREFIX}⏳ *Phase 1: Environment Setup Complete*

- **Node Alias**: ${alias}

Cloud scan triggered. Local script exited securely, process running in background.
```

---

### 模板 2：阶段二 - 云端扫描完成准备轮换 (`tpl_stage2_start`)
```markdown
${TEST_PREFIX}⏳ *Phase 2: Cloud Scan Complete*

- **Node Alias**: ${alias}

CSV library ready. Starting automatic rotation and comprehensive checks...
```

---

### 模板 3：阶段二 - 云端扫描超时告警 (`tpl_stage2_timeout`)
```markdown
${TEST_PREFIX}⚠️ *Phase 2: Cloud Scan Timeout*

- **Node Alias**: ${alias}

Failed to find ${ip}.csv within 5 minutes. Async deployment aborted. Please check cloud pipeline.
```

---

### 1. SNI 域名阻断告警 (`tpl_sni_blocked`)
```markdown
${TEST_PREFIX}🚨 *SNI 域名阻断告警*

- *节点别名*: `${alias}`
- *受阻 SNI*: `${current_sni}`
- *探测网段*: `${cidr_v4}`
- *当前状态*: 🔴 TLS 握手失败 / 已被阻断

☁️ 正在触发云端集群热扫描自愈... 预计耗时 1~2 分钟。自愈完成后将自动推送新配置。
```

---

### 2. X-ray REALITY 节点凭据推送 (`tpl_node_config`)
```markdown
${TEST_PREFIX}${title}

- *节点别名*: `${alias}`
- *伪装域名*: `${host}`
- *目标 SNI*: `${sni}`

- *Quantumult X 配置*:
```text
${qx_config}
```

- *V2rayN / Clash Meta 配置*:
```text
${vless_uri}
```
```

---

### 3. 严重故障致命告警 (`tpl_alert_failure`)
```markdown
${TEST_PREFIX}🚨 *致命故障告警: 节点自愈失败*

- *节点别名*: `${alias}`
- *失败原因*: ${failure_reason}
- *故障影响*: ${impact}
- *处理建议*: ${recommendation}
```

---

### 4. 节点体检 - 完整报表 (`tpl_health_full`)
```markdown
${TEST_PREFIX}🩺 *X-ray 节点深度体检报告*

- *节点别名*: `${alias}`
- *体检结论*: ${conclusion}

🌐 *网络与 DNS 解析*
- *IPv4 地址*: `${ipv4}`
- *IPv6 地址*: `${ipv6}`
- *伪装域名*: `${host}`
- *解析状态*: ${domain_status}

📡 *出站网络延迟*
${ping_report}

🎯 *SNI 伪装域名状态*
- *当前 SNI*: `${sni}`
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
- *流控算法*: `${flow}`
- *用户 UUID*: `${uuid_masked}`
- *Reality 公钥*: `${pub_masked}`
- *Short ID*: `${sid}`
```

---

## 📱 实际渲染效果案例 (Showcase)

<details>
<summary><b>点击展开查看：Telegram 客户端实际接收效果预览</b></summary>

### 效果预览 1：节点凭据下发
> 🚀 **X-ray Node Deployed**
> 
> - **Node Alias**: `us_racknerd`
> - **Domain**: `api-metrics-v132.example.com`
> - **Target SNI**: `gateway.icloud.com`
> 
> - **Quantumult X Config**:
> ```text
> vless=api-metrics-v132.example.com:443, method=none, password=d3b07384-d113-4632-9c93-2997707fa148, obfs=shadowsocks, obfs-host=gateway.icloud.com, obfs-uri=/, fast-open=true, udp-relay=true, tag=us_racknerd
> ```
> 
> - **V2rayN / Clash Meta Config**:
> ```text
> vless://d3b07384-d113-4632-9c93-2997707fa148@api-metrics-v132.example.com:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=gateway.icloud.com&fp=chrome&pbk=5xOX...&sid=a1b2c3d4&type=tcp#us_racknerd
> ```

### 效果预览 2：深度体检报表
> 🩺 **X-ray Node Health Report**
> 
> - **Node Alias**: `us_racknerd`
> - **Conclusion**: 🟢 EXCELLENT
> 
> 🌐 **Network & DNS**
> - **IPv4**: `192.129.143.41`
> - **IPv6**: `None`
> - **Domain**: `api-metrics-v132.example.com`
> - **DNS Status**: 🟢 MATCH (192.129.143.41)
> 
> 📡 **Outbound Latency**
> - 🇨🇳 阿里杭州: `142ms`
> - 🇨🇳 腾讯上海: `138ms`
> - 🇺🇸 Cloudflare: `12ms`
> - 🌐 Google DNS: `14ms`
> 
> 🎯 **SNI Status**
> - **SNI**: `gateway.icloud.com`
> - **Status**: 200 OK
> - **Basis**: TLS 1.3 / H2
> - **Handshake**: 38ms
> - **Cert Valid**: VALID (72 days remaining)
> - **CDN**: Direct (No CDN detected)
> - **Rating**: EXCELLENT (98/100)
> 
> 💻 **System & Kernel**
> - **Uptime**: 5 days, 12 hours
> - **TCP Fast Open**: 🟢 ENABLED (3)
> - **TCP BBR**: 🟢 bbr
> - **UDP Status**: 🟢 OPEN
> 
> 🐳 **Container & Proxy**
> - **Container**: 🟢 RUNNING (Up 5 days)
> - **Flow**: `xtls-rprx-vision`
> - **UUID**: `d3b07384-****-****-****-2997707fa148`
> - **Pub Key**: `5xOX3jK...`
> - **Short ID**: `a1b2c3d4`

</details>
