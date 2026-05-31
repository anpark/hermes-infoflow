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
| `test_send_via_serverapi.py` | `prepare_outbound_message` + `ServerAPI.send_group_message_intent` direct smoke. Does **not** exercise `InfoflowSendService` preview enrichment. Does **not** need hermes-agent. |
| `test_send_intent_matrix.py` | Direct `ServerAPI.send_group_message_intent` / `send_private_message_intent` smoke matrix for Markdown, reply, links, @, and 200x200 image bytes. It verifies protocol routing, not service-level reply preview enrichment. |
| `test_file_to_url_send_matrix.py` | Direct `ServerAPI` matrix for file-to-URL image compatibility: Markdown + `image_paths` / `image_bytes`, reply + Markdown image, `format=text` native image, and plain auto native image. Prints selected payload families (`MD` / `IMAGE` / BOS upload/getUrl/HEAD). Use `--runtime-plugin` after deployment. |
| `simulate_inbound_webhook.py` | Encrypts and posts fake Infoflow webhook messages to the local gateway. Exercises parser + adapter + Bot/LLM + tools + outbound send. Useful for prompt/tool-behavior checks after deployment. |
| `test_send_via_standalone.py` | `standalone_send(...)` — the cron / out-of-process entry point. Does **not** need hermes-agent. |
| `test_send_via_adapter.py`   | `InfoflowAdapter.send(...)` — the live gateway entry point. **Requires** `~/.hermes/hermes-agent` checkout. The script monkey-patches `gateway.config.Platform` to add an `INFOFLOW` member so an unpatched mainline hermes-agent still works. |
| `probe_group_formats.py` | Real supported group format matrix for `MD`/`TEXT`, native `AT` including @all/specific mention combinations, `reply`, and `IMAGE`; attaches webhook echo summaries from local Hermes logs. |
| `probe_group_links.py` | Real supported group `LINK` body-item matrix, including `href`/`label`, multiple links, `AT`, `reply`, optional `IMAGE`, and optional `@all`. |
| `probe_private_formats.py` | Real private app-message matrix for `text`, `md`, `richtext`, `image`, `reply`, and link-only richtext. Private messages need recipient-side manual validation by case id. |
| `probe_contract_edges.py` | Exact-wire edge probes for outbound casing, `MIXED`, protocol-family mismatches, invalid group `LINK`, AT-only, reply without `imid`, empty-text reply, and optional private edge probes. |
| `probe_reply_counts.py` | Real group/private reply-count probes. Confirms group `message.reply` only accepts one object and private `reply[]` supports multiple targets. Private cases require recipient-side manual validation. |
| `probe_reply_preview_edges.py` | Exact-wire reply preview/content edge probes. Confirms group reply can omit/empty `preview`, group invalid `messageid` fails, and private reply omission/invalid `msgid` API behavior. Private cases require recipient-side manual validation. |

The reusable pieces for the probe scripts live in `_message_format_probe.py`:

- 200x200 pure-blue PNG generation through the production media pipeline.
- Group webhook echo extraction from `~/.hermes/logs/gateway.log` and `agent.log`.
- Safe payload redaction for base64 image content.
- Shared case recording and JSON result formatting.

The three send-path smoke scripts (`test_send_via_serverapi.py`,
`test_send_via_standalone.py`, and `test_send_via_adapter.py`) accept the same
flags:

```text
--group <id>            override INFOFLOW_OP_GROUP once
--text "..."            override the default timestamped marker
--mention "@chengbo05"  prepend an @-mention to the text (repeatable)
--mention-user <csv>    structured human mention user ids
--mention-agent <csv>   structured robot mention agent ids
--at-all                structured @all mention
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

Check self-mention filtering (the text should remain plain text, without a
structured self mention for the bot itself):

```bash
python scripts/sim/test_send_via_standalone.py \
    --mention "@chengbo5.1" --text "self mention filter check"
```

Verify `@all` resolves via metadata in both entry points:

```bash
python scripts/sim/test_send_via_adapter.py --at-all
python scripts/sim/test_send_via_standalone.py --at-all
```

Run the reusable message-format probes against the configured test group:

```bash
python scripts/sim/probe_group_formats.py --user chengbo05
python scripts/sim/probe_group_links.py --user chengbo05
python scripts/sim/probe_group_links.py --user chengbo05 --include-at-all
python scripts/sim/probe_contract_edges.py --user chengbo05
python scripts/sim/probe_reply_counts.py --group 4507088
python scripts/sim/probe_reply_preview_edges.py --group 4507088
```

Validate file-to-URL send compatibility after changing `file_to_url.py`,
`file_delivery.py`, `serverapi.py`, or send-format routing:

```bash
# Source tree behavior.
python scripts/sim/test_file_to_url_send_matrix.py \
    --group 4507088 --private-user chengbo05

# Deployed runtime plugin behavior after scripts/deploy.sh.
python scripts/sim/test_file_to_url_send_matrix.py \
    --runtime-plugin --group 4507088 --private-user chengbo05
```

The expected routing is:

- Markdown + `image_paths` -> BOS upload/getUrl/HEAD -> `MD` with `![alt](url)`.
- `reply_to` + Markdown + `image_paths` -> `TEXT` reply packet, then `MD`.
- `format=text` + `image_paths` -> native `IMAGE` packet with `TEXT`.
- plain auto text + `image_paths` -> native `IMAGE` packet with `TEXT`.
- private Markdown + `image_bytes` -> staged temp image, BOS URL, private `md`.

Simulate inbound webhook messages when prompt/tool behavior needs full gateway
coverage:

```bash
# Requires the Hermes gateway to be running locally.
python scripts/sim/simulate_inbound_webhook.py \
    --case all --group 4507088

# Faster single-case examples.
python scripts/sim/simulate_inbound_webhook.py --case dm-file --group 4507088
python scripts/sim/simulate_inbound_webhook.py --case dm-group-md-image --group 4507088
python scripts/sim/simulate_inbound_webhook.py --case group-native-image --group 4507088
```

`simulate_inbound_webhook.py` is intentionally different from direct
`ServerAPI` probes: it depends on the live gateway, invokes the configured LLM,
and can take several seconds per case. Use its marker (`PROMPTSIM|...`) to
find outbound messages and logs.

Override the group once without changing `~/.hermes/.env`:

```bash
python scripts/sim/probe_group_formats.py --group 4507088 --user chengbo05
```

Include robot AT probes by passing a non-self agent id:

```bash
python scripts/sim/probe_group_formats.py \
    --group 4507088 --user chengbo05 --agent-id 17212
```

Run private-format probes against a recipient:

```bash
python scripts/sim/probe_private_formats.py --user chengbo05
python scripts/sim/probe_contract_edges.py --user chengbo05 \
    --include-private --private-user chengbo05
python scripts/sim/probe_reply_counts.py --group 4507088 \
    --include-private --private-user chengbo05
python scripts/sim/probe_reply_preview_edges.py --group 4507088 \
    --include-private --private-user chengbo05
```

Private self-sends do not produce a reliable local webhook echo. The scripts
embed case ids (`P01`, `P02`, ..., `P17`) and Chinese expected-display text in
each message; ask the recipient to confirm each case by id. As of the
2026-05-28 validation, `P01`-`P17` have recipient-side display confirmation.
Notable confirmed edges include link-only richtext (`P09`), link-only richtext
with `reply[]` (`P10`), two richtext links (`P11`), empty-text reply-only
(`P12`), two text reply targets (`P13`), and link-only richtext with two reply
targets (`P14`), text with three/five reply targets (`P15`/`P16`), and
link-only richtext with five reply targets (`P17`).
The reply-preview edge probe has additional private ids `P01`-`P04` for
no-content/empty-content and invalid-msgid reply behavior; as of marker
`20260528-195317`, `P01`/`P02` were confirmed to display the reply normally
without `content` or with empty `content`; `P03`/`P04` were confirmed to show
the message body normally while the reply area displays an error state for the
invalid `msgid`.

## Notes

- These scripts hit the real Infoflow API and post real messages to the
  configured test group. Don't aim them at production-only groups.
- `test_file_to_url_send_matrix.py --runtime-plugin` imports the deployed
  plugin from `~/.hermes/plugins/infoflow`; without the flag it imports the
  in-tree source.
- `simulate_inbound_webhook.py` posts to `http://127.0.0.1:<INFOFLOW_PORT>/<INFOFLOW_WEBHOOK_PATH>`
  and uses `INFOFLOW_ENCODING_AES_KEY` to encrypt fake inbound messages.
- `probe_group_formats.py` intentionally includes expected API failures such
  as `MD` header + `TEXT` body and `IMAGE` packet + `MD` text item.
- `probe_group_links.py` intentionally includes expected API failures such as
  label-only `LINK` and `MD + LINK`.
- `probe_contract_edges.py` intentionally includes expected API failures such
  as lowercase group `msgtype`, lowercase group body `type`, outbound `MIXED`,
  uppercase private `msgtype`, and uppercase private content object keys.
- `probe_reply_counts.py` intentionally includes expected group API failures:
  `message.reply` arrays with 2 or 3 items fail even under `msgtype=TEXT`.
  The same script can send private 3/5-reply probes with `--include-private`.
- `probe_reply_preview_edges.py` intentionally includes expected group API
  failures for invalid reply `messageid`. Private invalid `msgid` cases are
  API-accepted; the 2026-05-28 validation confirmed that the message body
  still displays while the reply area shows an error state.
- Group probes validate semantics with webhook echo fields. A send result is
  not enough: `MD + reply` can send successfully while losing `replyData`.
- Direct `ServerAPI` smoke scripts intentionally do not read local message
  stores. A group reply without explicit `preview` can preserve `replyData`
  while showing Infoflow's generic quote summary. Use `InfoflowSendService`,
  the adapter path, the tool path, or an explicit `preview` when validating
  application-level quote summaries.
- Raw probes on 2026-05-29 confirmed reply identity fields: group
  `message.reply.imid` and private `reply[].uid` must use the quoted message
  sender imid, which appears in webhook raw fields as `fromid`/`FromId`, for
  the client quote card to display the original sender. Do not use the current
  robot id as a fallback unless the quoted message was sent by that robot; do
  not use `"0"` or the uuapName as the normal-path identity value.
- Raw group preview probes on 2026-05-29 confirmed that request preview can be
  very long, but echo/client display keeps at most the first 100 characters and
  appends ASCII `...` when longer. Application-level auto preview generation
  should match this visible limit.
- Application-level quote summary enrichment reads the unified `MessageStore`,
  which records both inbound messages and locally sent outgoing messages.
  `SentMessageStore` is a recent-sent index for dedup, reply-to-self detection,
  and recall; it is not the source of message body content.
- Pure group `IMAGE` is included as an API-acceptance probe, but local webhook
  echo may be absent; combinations such as `AT + IMAGE`, `LINK + IMAGE`, and
  `reply + IMAGE` are the echo-verified image semantics.
- Group `@all` and specific user/bot mentions have special behavior:
  `TEXT`/`IMAGE` can preserve both when they are separate `AT` items; putting
  them in the same `AT` item drops the specific mention. `MD` cannot reliably
  preserve both as native mentions.
- Network failures or auth errors are reported in the result JSON; the
  process exits non-zero on failure so the scripts compose well in CI
  or shell pipelines.
- `_env.py` deliberately avoids `python-dotenv` so the scripts run on a
  vanilla interpreter without extra installs.
