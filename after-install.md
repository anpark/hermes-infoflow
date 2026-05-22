# hermes-infoflow installed

Normalize the cloned plugin once so this install matches `deploy.sh`,
`hermes-infoflow-tools update`, and pip-style deploys:

```bash
bash ~/.hermes/plugins/infoflow/scripts/normalize.sh
hermes gateway restart
```

Use `--port <PORT>` on the normalize command if the webhook port should be
written to `~/.hermes/.env`.
