# @chbo297/hermes-infoflow-tools

`npx` launcher for installing/updating the [`hermes-infoflow`](https://github.com/chbo297/hermes-infoflow)
plugin into a Hermes Agent home directory.

> **Requires Python 3.11+ and pip on the host.** `hermes-infoflow` is a Python
> plugin whose deploy orchestrator (`hermes_infoflow/deploy.py`) must run under
> a host Python interpreter. This npm package is a *thin launcher*: it detects
> Python, downloads the plugin sdist from PyPI, and runs `deploy.py`. It does
> not remove the Python dependency — it only gives Node users an `npx` entry
> point as an alternative to `pipx run hermes-infoflow-tools`.

## Usage

```bash
# default: pip download + tar + normalize into ~/.hermes/plugins/infoflow/
npx -y @chbo297/hermes-infoflow-tools update --version <version>
npx -y @chbo297/hermes-infoflow-tools update --version <version> --port 9000

# normalize a prior `hermes plugins install` clone into the same layout
npx -y @chbo297/hermes-infoflow-tools normalize
npx -y @chbo297/hermes-infoflow-tools normalize --port 9000
```

Pin the plugin package to a stable version (mirrors the `pipx` flow):

```bash
npx -y @chbo297/hermes-infoflow-tools update --version 2026.6.11 --mode extract --port 9000
```

## Options

`update`:

| Flag | Description |
| --- | --- |
| `--version <version>` | PyPI version specifier (default: `latest`) |
| `--index-url <url>` | PyPI index URL (default: `https://pypi.org/simple`) |
| `--package-name <name\|path>` | Plugin package on PyPI, or a local checkout path |
| `--channel-id <id>` | Plugin id under `~/.hermes/plugins/` (only `infoflow`) |
| `--mode <extract\|pip>` | `extract` (default); `pip` is a deprecated alias |
| `--port <1-65535>` | Webhook port, written to `~/.hermes/.env` |
| `--python <path>` | Explicit Python interpreter to use |
| `--dry-run` | Print commands without executing them |

`normalize`: `--source <dir>`, `--channel-id <id>`, `--port`, `--python`, `--dry-run`.

This is the Node equivalent of the Python `hermes-infoflow-tools` CLI; both hand
off to the same `hermes_infoflow/deploy.py` orchestrator, so the resulting
deployment is identical.

## Publishing (manual)

```bash
cd tools/hermes-infoflow-tools-npm
npm publish --access public
```

Keep `version` in `package.json` in sync with the plugin release version.
