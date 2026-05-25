# hermes-infoflow-tools

CLI helper to install/update the `hermes-infoflow` plugin into a Hermes Agent
home directory.

Every complete install path first aligns `~/.hermes/hermes-agent` to
`chbo297/hermes-agent` branch `fix/send-message-plugin-target-routing`, then
verifies the gateway Python imports `gateway` from that checkout before the
plugin directory is replaced.

```bash
# default: pip download + tar + normalize into ~/.hermes/plugins/infoflow/
pipx run hermes-infoflow-tools update --version <version>
pipx run hermes-infoflow-tools update --version <version> --port 9000
```

Pin both the installer package and the plugin package to a stable version:

<!-- sync:hermes-infoflow-version:latest -->
```bash
pipx run --spec hermes-infoflow-tools==2026.5.26 hermes-infoflow-tools update --version 2026.5.26 --mode extract --port 9000
```
<!-- /sync:hermes-infoflow-version:latest -->

Beta / prerelease: use the exact PEP 440 version. For newly published betas,
`--no-cache` avoids stale pipx/uv resolver cache:

<!-- sync:hermes-infoflow-version:beta -->
```bash
pipx run --no-cache --spec hermes-infoflow-tools==2026.5.26b1 hermes-infoflow-tools update --version 2026.5.26b1 --mode extract --port 9000
```
<!-- /sync:hermes-infoflow-version:beta -->

```bash
# deprecated compatibility alias; still deploys directory-style to ~/.hermes/plugins/infoflow/
pipx run hermes-infoflow-tools update --version <version> --mode pip
pipx run hermes-infoflow-tools update --version <version> --mode pip --port 9000

# normalize a prior `hermes plugins install` clone into the same layout
pipx run hermes-infoflow-tools normalize
pipx run hermes-infoflow-tools normalize --port 9000
```

See the main repo README for the full installation matrix and trade-offs
between the four installation paths.
