# hermes-infoflow simulation scripts

End-to-end smoke tests that exercise the **real** Infoflow backend so
you can verify the plugin's outbound code paths after refactors.

They all share the same bootstrap (`_env.py`):

1. Loads `~/.hermes/.env` into `os.environ` (without overriding values
   already exported in the shell).
2. Prepends `~/.hermes/hermes-agent` to `sys.path` if present, so the
   live `gateway` package is importable.
3. Prepends the repo root to `sys.path` so the in-tree
   `hermes_infoflow` (the one you just edited) wins over any
   pip-installed copy.

## Required env vars

These are read from `~/.hermes/.env` (or your shell):

| Variable | Notes |
| --- | --- |
| `INFOFLOW_API_HOST` | optional; defaults to `https://api.im.baidu.com` |
| `INFOFLOW_APP_KEY` | from the Infoflow open-platform console |
| `INFOFLOW_APP_SECRET` | from the Infoflow open-platform console |
| `INFOFLOW_APP_AGENT_ID` | optional but required for some calls |
| `INFOFLOW_CHECK_TOKEN` / `INFOFLOW_ENCODING_AES_KEY` | only used for inbound |
| `INFOFLOW_OP_GROUP` | **the single numeric group id used by these sim scripts** |

## Entry points

| Script | What it exercises |
| --- | --- |
| `test_send_via_serverapi.py` | `prepare_outbound_message` + `ServerAPI.send_to_group` — the shared code path both other entry points converge on. Does **not** need hermes-agent. |
| `test_send_via_standalone.py` | `standalone_send(...)` — the cron / out-of-process entry point. Does **not** need hermes-agent. |
| `test_send_via_adapter.py`   | `InfoflowAdapter.send(...)` — the live gateway entry point. **Requires** `~/.hermes/hermes-agent` checkout. The script monkey-patches `gateway.config.Platform` to add an `INFOFLOW` member so an unpatched mainline hermes-agent still works. |

All three accept the same flags:

```text
--group <id>            override INFOFLOW_OP_GROUP once
--text "..."            override the default timestamped marker
--mention "@chengbo05"  prepend an @-mention to the text (repeatable)
--mention-user <csv>    metadata.mention_user_ids
--mention-agent <csv>   metadata.mention_agent_ids
--at-all                metadata.at_all = true
```

## Recipes

Send the default smoke message via each path:

```bash
python scripts/sim/test_send_via_serverapi.py
python scripts/sim/test_send_via_standalone.py
python scripts/sim/test_send_via_adapter.py
```

Test the previously-broken cron @-mention path:

```bash
python scripts/sim/test_send_via_standalone.py \
    --mention "@chengbo05" --text "cron @-mention parity check"
```

Check self-mention filtering (the text should remain plain text, without an
`at-agent` payload for the bot itself):

```bash
python scripts/sim/test_send_via_standalone.py \
    --mention "@chengbo5.1" --text "self mention filter check"
```

Verify `@all` resolves via metadata in both entry points:

```bash
python scripts/sim/test_send_via_adapter.py --at-all
python scripts/sim/test_send_via_standalone.py --at-all
```

## Notes

- These scripts hit the real Infoflow API and post real messages to the
  configured test group. Don't aim them at production-only groups.
- Network failures or auth errors are reported in the result JSON; the
  process exits non-zero on failure so the scripts compose well in CI
  or shell pipelines.
- `_env.py` deliberately avoids `python-dotenv` so the scripts run on a
  vanilla interpreter without extra installs.
