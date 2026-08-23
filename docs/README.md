[English](README.md) · [简体中文](README_zh.md)

### Snack Connoisseur

Automated cloud instance orchestration and node lifecycle management framework with zero-secret edge isolation and continuous self-healing capabilities.

[Architecture](#architecture-overview) · [Installation](#installation--setup) · [Usage Guide](#usage-guide) · [Scenarios](#practical-scenarios) · [Templates](#template-specifications) · [Directory Structure](#directory-structure) · [Environment Variables](#environment-variables) · [License](#license)


### Architecture Overview

![Snack Connoisseur Architecture Diagram](../assets/architecture.svg)

[Workflow Canvas (workflow.canvas)](../assets/workflow.canvas) — Covers control plane, batch provisioning, self-healing rotation, health audit, remote infrastructure, and alerting mechanisms.


### Installation & Setup

**Prerequisites**
* Python >= 3.12 (managed via `uv`)
* OpenSSH client, Bash, Git, and curl

**Quick Setup**

```bash
# 1. Clone repository
git clone https://github.com/your-username/snack-connoisseur.git
cd snack-connoisseur

# 2. Grant executable permissions
chmod +x cnsr.sh

# 3. Initialize Python 3.12 virtual environment & dependencies
uv sync

# 4. Configure local environment variables
cp .env.example .env
# Edit .env and supply your AWS, Cloudflare, and Telegram credentials
```


### Usage Guide

```text
Snack Connoisseur (cnsr)

USAGE:
  ./cnsr.sh <command> [alias] [arguments] [options]

COMMANDS:
  init <alias> [ip]            Provision generic Linux VPS node (Options: --harden, --debug)
  init-aws <alias>             Provision AWS Lightsail node (Options: --region, --count, --bundle, --blueprint)
  init-aws -f <conf>           Batch provision AWS nodes from preset template
  destroy-aws <alias|pattern>  Destroy AWS Lightsail instance(s) and clean DNS/SSH config (Option: --region)
  check <alias>                Comprehensive health check: latency, TLS quality, BBR, container
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
  --region <region>            Specify AWS region (default: ap-northeast-1)
  --count <N>                  Specify node count for AWS initialization (Max: 20)
  --key-pair <name>            Specify AWS key pair name (for init-aws)
  -f, --file <conf>            Specify batch config file for init-aws
  -h, --help, -help            Show this help manual

EXAMPLES:
  ./cnsr.sh init sg_aws 198.51.100.1 --harden                        # Initialize single generic node with hardening
  ./cnsr.sh init-aws aws-node --region ap-northeast-1 --count 3     # Provision 3 AWS nodes in Tokyo
  ./cnsr.sh init-aws -f jp_aws-lightsail --count 3                   # Batch provision 3 nodes from preset
  ./cnsr.sh init-aws -f jp_aws-lightsail --count 3 --detach          # Detached batch deploy (safe to close terminal)
  ./cnsr.sh destroy-aws "jp_aws-lightsail-*" --region ap-northeast-1 # Batch destroy matching instances
  ./cnsr.sh check sg_aws                                             # Execute comprehensive node diagnostic
  ./cnsr.sh test-tg sg_aws                                           # Send sample Telegram alert cards
  ./cnsr.sh test-sni sg_aws                                          # Dry-run SNI evaluation
  ./cnsr.sh lang en                                                  # Switch CLI language to English
```


### Practical Scenarios

* **Scenario 1: Batch Provision 5 Tokyo Nodes (Detached)** — Deploy nodes via preset configuration in background with Telegram progress notifications.
```bash
./cnsr.sh init-aws -f jp_aws-lightsail --count 5 --detach
```

* **Scenario 2: Node Health Audit & Latency Benchmark** — Inspect network latency, TLS quality, BBR congestion control, and container health.
```bash
./cnsr.sh check jp_aws-lightsail-1
```

* **Scenario 3: One-Click IP Reallocation on Network Block** — Release old IP, allocate new static IP, and cascade update DNS and SSH configs.
```bash
./cnsr.sh rotate-ip jp_aws-lightsail-1
```

* **Scenario 4: Periodic Fingerprint & Camouflage Domain Rotation** — Refresh UUID, x25519 keypair, ShortID, and SNI camouflage domain.
```bash
./cnsr.sh rotate-sni jp_aws-lightsail-1
./cnsr.sh rotate-dns jp_aws-lightsail-1
```

* **Scenario 5: Batch Cluster Teardown** — Safely terminate matching instances, release static IPs, and purge Cloudflare/SSH records.
```bash
./cnsr.sh destroy-aws "jp_aws-lightsail-*" --region ap-northeast-1
```


### Template Specifications

| Category | Files | Description |
| :--- | :--- | :--- |
| **Node Presets** | `jp_aws-lightsail.conf`<br>`sg_aws-lightsail.conf`<br>`us_aws-lightsail.conf` | Region, machine tier (`nano_3_0`), blueprint, dual-stack network (`dual`), and firewall specifications |
| **Service & Protocol** | `compose.yml.template`<br>`config.json.template` | Docker container orchestration and protocol runtime configuration |
| **Self-Healing Probes** | `runner.template.sh`<br>`reality_rotate.template.sh`<br>`reality_check.template.sh`<br>`async_deploy.template.sh` | 15-minute cron watchdog shim, 7-tier SNI selector, native TLS ClientHello probe, and detached deploy chain |
| **Telegram Alerts** | `tg_templates.sh`<br>`tg_templates.md` | Formatted multi-scenario notification cards (Node Ready, Rotated, Health, Alarm, Failure) |

```bash
# Example: templates/jp_aws-lightsail.conf
ALIAS_BASE="jp_aws-lightsail"      # Node alias prefix
REGION="ap-northeast-1"            # AWS datacenter region
BUNDLE_ID="nano_3_0"               # Hardware bundle tier ($3.50/month)
BLUEPRINT_ID="debian_12"           # OS distribution blueprint
KEY_PAIR_NAME=""                   # Optional specified AWS key pair
HOST_FIREWALL="ufw"                # Host firewall type (ufw)
IP_STACK="dual"                    # Network IP stack (dual / ipv4 / ipv6)
PORTS_TCP="22,443"                 # Allowed inbound TCP ports
PORTS_UDP="443"                    # Allowed inbound UDP ports
COUNT=1                            # Default batch instance count
```

Other templates can be inspected directly under `templates/*`.


### Directory Structure

```text
snack-connoisseur/
├── assets/                     # Architecture diagrams (SVG)
├── core/                       # Core provisioning, check, update, and rotation scripts
├── gateway/                    # Zero-secret edge proxy gateway (Cloudflare Worker)
├── lib/                        # Security sanitization, SSH helpers, and UI renderers
├── providers/                  # Cloud provider API drivers (AWS Lightsail SDK)
├── templates/                  # Node configuration and alerting message templates
├── tests/                      # Pytest automated test suite
├── tools/                      # Native probe utilities (reality-checker)
├── cnsr.sh                     # Main CLI control script
├── LICENSE                     # MIT Open Source License
└── pyproject.toml              # Project dependency and runtime configuration
```


### Environment Variables

| Variable | Purpose | Required By |
| :--- | :--- | :--- |
| `AWS_ACCESS_KEY_ID` | AWS IAM Access Key ID | `init-aws`, `destroy-aws`, `rotate-ip` |
| `AWS_SECRET_ACCESS_KEY` | AWS IAM Secret Access Key | `init-aws`, `destroy-aws`, `rotate-ip` |
| `CF_API_TOKEN` | Cloudflare DNS Edit API Token | `init`, `init-aws`, `rotate-dns`, `rotate-ip` |
| `CF_ZONE_ID` | Cloudflare Domain Zone ID | `init`, `init-aws`, `rotate-dns`, `rotate-ip` |
| `TG_BOT_TOKEN` | Telegram Bot Token for Alerting | `check`, `test-tg`, remote alerts |
| `TG_CHAT_ID` | Telegram Chat ID for Alerting | `check`, `test-tg`, remote alerts |
| `GH_TOKEN` | GitHub Personal Access Token | Fallback Cloud Scanner Dispatch |
| `GATEWAY_URL` | Cloudflare Worker Zero-Key Gateway URL | Secure Cloud Scanner Dispatch |
| `GATEWAY_AUTH_KEY` | Bearer Auth Key for Worker Gateway | Secure Cloud Scanner Dispatch |
| `CNSR_LANG` | Default console interface language (`en` or `zh`) | All CLI interactions |


### License

MIT License — Do what you want, but respect the boundaries.
