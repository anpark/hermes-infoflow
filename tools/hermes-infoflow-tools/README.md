# hermes-infoflow-tools

CLI helper to install/update the `hermes-infoflow` plugin into a Hermes Agent
home directory.

```bash
# default: pip download + tar + rsync into ~/.hermes/plugins/infoflow/
pipx run hermes-infoflow-tools update --version 0.2.0

# or, install into site-packages and load via entry-point
pipx run hermes-infoflow-tools update --version 0.2.0 --mode pip
```

See the main repo README for the full installation matrix and trade-offs
between the four installation paths.
