# Snack Connoisseur

## Architecture

![Snack Connoisseur Architecture Diagram](assets/architecture.svg)

---

## Usage

```text
Snack Connoisseur (cnsr) - X-ray REALITY Controller & Anti-Censorship Suite

USAGE:
  ./cnsr.sh <command> [subcommand] [alias] [arguments] [options]

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
```

