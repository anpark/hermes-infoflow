# hermes-infoflow-tools

CLI helper to install/update the `hermes-infoflow` plugin into a Hermes Agent
home directory.

```bash
# default: pip download + tar + rsync into ~/.hermes/plugins/infoflow/
pipx run hermes-infoflow-tools update --version 2026.5.21
pipx run hermes-infoflow-tools update --version 2026.5.21 --port 9000

# or, install into site-packages and load via entry-point
pipx run hermes-infoflow-tools update --version 2026.5.21 --mode pip
pipx run hermes-infoflow-tools update --version 2026.5.21 --mode pip --port 9000
```

See the main repo README for the full installation matrix and trade-offs
between the four installation paths.
