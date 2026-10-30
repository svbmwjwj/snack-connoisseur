# cnsr

`cnsr` sets up and hardens servers, and can deploy a self-hosted Xray REALITY node on them that changes its SNI on its own when that SNI gets blocked.

Blocking is detected from the client side: a Quantumult X task on your device checks each node from where you are. When the check fails, the node switches to a backup SNI, and Quantumult X notifies you to update the config. UUIDs and keys stay the same across switches.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) on the machine you run `cnsr` from (macOS or Linux; Windows is not supported for now).
- A domain on Cloudflare, and an API token with DNS edit, Workers Scripts edit and Workers KV edit permissions. `cnsr` checks each permission on first run and names any that are missing.
- For a new AWS server: an AWS access key that can manage Lightsail.
- For a new Hetzner server: a Hetzner Cloud API token with read and write access. `cnsr` reads `HCLOUD_TOKEN` from the environment first, then the token it saved earlier, and asks for one when neither is there.
- For an existing server: SSH access to it.

## Usage

```bash
uv sync
uv run cnsr
```

`cnsr` without a command opens a menu: deploy a server, remove a server, or list the servers. Each entry is also a command:

| Command | What it does |
| --- | --- |
| `cnsr up <alias> --aws <region> --size <size>` | Creates a new Lightsail instance in any of Lightsail's 19 regions (`jp-tokyo`, `sg`, `us-oregon`, `de-frankfurt`, ...; the menu shows three and lists the rest under more regions; `hk`, `id-jakarta`, `my` and `es` must be enabled in your AWS account first) and deploys the features you tick |
| `cnsr up <alias> --hetzner <region> --size <type>` | Creates a new Hetzner Cloud server in `sg`, `us-oregon`, `us-virginia`, `de-falkenstein`, `de-nuremberg` or `fi-helsinki`, behind a cloud firewall, and deploys the features you tick |
| `cnsr up <alias> --ip <ip>` | Takes over an existing server and deploys the features you tick |
| `cnsr up <alias>` | On an existing alias: resumes unfinished steps, adds features you tick now, tops up backup SNIs and updates the server |
| `cnsr rm [alias]` | Removes the server; on AWS it also deletes the instance and releases the static IP that `cnsr` created; on Hetzner it deletes the server, its Primary IP and its firewall, and keeps the SSH key. Without an alias it lists every server to tick one or more. It shows what will go, then asks once to confirm (Enter cancels) |
| `cnsr list` | Prints the servers and what each one runs, with this month's traffic for Hetzner servers; with an Xray node, also the Quantumult X config lines and the v2rayN subscription URL |

`cnsr up` asks which features to deploy. Basic hardening is the base; the Xray node is an extension and needs it, so ticking the Xray node ticks the hardening too, and unticking the hardening unticks the Xray node. Ticking only the hardening sets up the server and leaves every other port closed. Without arguments, `up` asks for each setting.

`--to base` deploys only the hardening and `--to xray` deploys the hardening and the Xray node, without the feature menu. `--size` with `--aws` names the least cores and GB of memory the server needs, for example `2c0.5g` or `2c1g`, and `cnsr` takes the cheapest bundle that meets it; without it, a menu shows three common sizes and a fourth entry, more sizes, that lists every size the region sells. `--size` with `--hetzner` names the server type, for example `cx23`, `cpx22` or `ccx13`; without it, `up` asks as Hetzner's console does: first the plan (Cost-Optimized, Regular Performance, Dedicated) with how many locations sell it, then the location (one that does not sell the plan now cannot be picked; skipped when `--hetzner` names it), then the server types of the plan sold there. Each size line shows its CPU, memory, disk, included traffic and monthly price. In any menu after the first question, Esc goes back one step. Without a terminal, AWS takes `2c0.5g` and Hetzner needs `--size`. `--static-ip <ip>`, with `--aws`, makes a static IP you already have in the region the server's address: one attached to another instance is moved only after you agree, and `cnsr rm` detaches it and keeps it. Without it, `up` in a terminal lists the region's existing static IPs in a menu; the first entry, a new static IP that `cnsr rm` releases, is the default. Picking an IP another `cnsr` server uses makes the new server take over from it: once the new instance runs, the IP moves to it and the old server goes offline (a failed first deployment moves it back). After the deployment, `cnsr rm` the old server yourself; the IP stays with the new one. `--static-ip` with such an IP stops instead.

For a new server, `up` prints the equivalent command before the first deploy step, for example `cnsr up sg-2 --aws sg --size 2c0.5g --to xray`. Run it with another alias to set up one more server the same way. It never contains a token or key, nor `--static-ip`: a static IP serves one server only.

Run `uv run cnsr --help` for every option.

The SNI scan runs from your own computer while an Xray node is deployed, so it has to reach the internet directly: turn off any local proxy or TUN mode before running `up` with the Xray node.

## Privacy

Server addresses, SNIs and keys are stored only in your Cloudflare account and on your own machine. The subscription URLs carry your client token, so share them only with people you trust.

## Development

```bash
uv run pytest
```

```bash
uv run node --test tests/worker/*.test.mjs
```

## License

MIT
