# cnsr

`cnsr` sets up and hardens servers, and can deploy a self-hosted Xray REALITY node on them that changes its SNI on its own when that SNI gets blocked.

Blocking is detected from the client side: a Quantumult X task on your device checks each node from where you are. When the check fails, the node switches to a backup SNI, and Quantumult X notifies you to update the config. UUIDs and keys stay the same across switches.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) on the machine you run `cnsr` from (macOS or Linux; Windows is not supported for now).
- A domain on Cloudflare, and an API token with DNS edit, Workers Scripts edit and Workers KV edit permissions. `cnsr` checks each permission on first run and names any that are missing.
- For a new AWS server: an AWS access key that can manage Lightsail.
- For an existing server: SSH access to it.

## Usage

```bash
uv sync
uv run cnsr
```

`cnsr` without a command opens a menu: deploy a server, remove a server, or list the servers. Each entry is also a command:

| Command | What it does |
| --- | --- |
| `cnsr up <alias> --aws <region>` | Creates a new Lightsail instance in `jp`, `sg` or `us` and deploys the features you tick |
| `cnsr up <alias> --ip <ip>` | Takes over an existing server and deploys the features you tick |
| `cnsr up <alias>` | On an existing alias: resumes unfinished steps, adds features you tick now, tops up backup SNIs and updates the server |
| `cnsr rm <alias>` | Removes the server; on AWS it also deletes the instance and releases the static IP that `cnsr` created |
| `cnsr list` | Prints the servers and what each one runs; with an Xray node, also the Quantumult X config lines and the v2rayN subscription URL |

`cnsr up` asks which features to deploy. Basic hardening is the base; the Xray node is an extension and needs it. Ticking only the hardening sets up the server and leaves every other port closed. Without arguments, `up` asks for each setting.

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
