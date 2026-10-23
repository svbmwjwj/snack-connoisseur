# cnsr

`cnsr` deploys self-hosted Xray REALITY nodes with one command and changes a node's SNI on its own when that SNI gets blocked.

Blocking is detected from the client side: a Quantumult X task on your device checks each node from where you are. When the check fails, the node switches to a backup SNI, and Quantumult X notifies you to update the config. UUIDs and keys stay the same across switches.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) on the machine you run `cnsr` from (macOS or Linux; Windows is not supported for now).
- A domain on Cloudflare, and an API token with DNS edit, Workers Scripts edit and Workers KV edit permissions. `cnsr` checks each permission on first run and names any that are missing.
- For a new AWS node: an AWS access key that can manage Lightsail.
- For an existing server: SSH access to it.

## Usage

```bash
uv sync
uv run cnsr add
```

`cnsr add` with no arguments asks for each setting. The other commands are:

| Command | What it does |
| --- | --- |
| `cnsr add <alias> --aws <region>` | Creates a new Lightsail instance in `jp`, `sg` or `us` and deploys a node on it |
| `cnsr add <alias> --ip <ip>` | Takes over an existing server |
| `cnsr add <alias>` | On an existing alias: resumes unfinished steps, tops up backup SNIs and updates the node |
| `cnsr rm <alias>` | Removes the node; on AWS it also deletes the instance and releases the static IP that `cnsr` created |
| `cnsr list` | Prints the nodes, the Quantumult X config lines and the v2rayN subscription URL |

Run `uv run cnsr --help` for every option.

The SNI scan runs from your own computer during deployment, so it has to reach the internet directly: turn off any local proxy or TUN mode before running `add`.

## Privacy

Node addresses, SNIs and keys are stored only in your Cloudflare account and on your own machine. The subscription URLs carry your client token, so share them only with people you trust.

## Development

```bash
uv run pytest
```

```bash
uv run node --test tests/worker/*.test.mjs
```

## License

MIT
