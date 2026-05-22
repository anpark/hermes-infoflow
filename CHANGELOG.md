# Changelog

All notable changes to `hermes-infoflow` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/)
versioning (with prerelease suffixes such as `0.1.0b1` for betas).

## [Unreleased]

### Added

- `scripts/deploy.sh` and `scripts/lib/deploy-common.sh` accept `--port` to
  set `INFOFLOW_PORT` in `~/.hermes/.env`; without `--port`, an existing
  value is preserved and the default `26521` is seeded only when missing.
- `hermes-infoflow-tools update` accepts `--port` for both `extract` and
  `pip` modes (extract forwards to `deploy-common.sh`; pip writes `.env`
  via bundled `env_editor`).
- `scripts/lib/edit_hermes_env.py` for safe `.env` upserts during deploy.
- Deploy / installer config editing now ensures
  `platform_toolsets.infoflow` includes the same baseline tool permissions
  as CLI sessions, including the `hermes-infoflow` toolset.

### Changed

- Default webhook listen port (`DEFAULT_PORT` / `INFOFLOW_PORT`) is now
  **26521** (was 8646).

## [2026.5.21] - 2026-05-21

### Added

- Group chat processing emoji (`d135` 敲键盘): when the bot is directly
  @-mentioned or engaged via the follow-up window, it now adds an emoji
  reaction to the inbound message immediately and removes it once the LLM
  reply finalizes (sent, `NO_REPLY` suppression, refusal suppression, send
  error, or 10-minute fallback timeout).
- Private chat (DM) processing emoji: every inbound DM now shows the same
  processing indicator. The emoji is removed when the bot sends a reply or
  finalizes silently.
- Emoji reaction REST client (`add_message_reaction` /
  `delete_message_reaction`) supporting both group (`chatType=2`, requires
  `chatId`) and service-account DM (`chatType=7`, requires `fromUid`,
  omits `chatId`) payload shapes; `msgId2` is optional per Infoflow doc.
- Webhook parser now extracts `MsgId2` (DM) and `msgid2` (group) from the
  top-level payload, threads it through `IncomingMessage`, the SQLite
  message store (with backward-compatible `ALTER TABLE` migration) and
  the recall inbound context, while keeping it out of the LLM-facing
  message body.
- Documentation: `docs/emoji-api.md` reorganized by group/DM scenarios
  with the full webhook-payload-to-API field mapping.

### Fixed

- Processing emoji is no longer deleted prematurely. Cleanup is now driven
  by the actual outbound send paths via a `ContextVar`-propagated promise
  (plus a 10-minute fallback timer) instead of the dispatch `finally`
  block — which used to fire as soon as `adapter.handle_message` returned,
  long before the LLM reply was produced.
- `message_store` reads of `group_messages` now use explicit column lists
  instead of `SELECT *`, keeping the row → `GroupMessageRecord` mapping
  correct after the `msgid2` `ALTER TABLE` migration appends the new
  column at the end of the physical schema.

## [0.2.2] - 2026-05-20

### Fixed

- First successful upload of ``hermes-infoflow-tools`` to PyPI (Trusted
  Publisher for the sibling project had not been registered when 0.2.0
  and 0.2.1 tags were pushed).
- Remove accidentally committed ``scripts/test_prompts_results.json``
  test-output artifact and add it to ``.gitignore``.

## [0.2.1] - 2026-05-20

### Fixed

- ``hermes-infoflow-tools`` sdist build failed on PyPI due to an invalid
  classifier ``Topic :: Software Development :: Installation/Setup`` —
  corrected to ``Topic :: System :: Installation/Setup``.
- ``publish.yml`` now passes ``skip-existing: true`` to
  ``pypa/gh-action-pypi-publish`` so partial-failure reruns don't break
  on already-published versions.

## [0.2.0] - 2026-05-20

### Added — OpenClaw parity pass

- **Own-message guard.** Inbound events whose root ``fromid`` equals the
  bot's persisted robotId are now dropped before policy evaluation. The
  bot's robotId is auto-discovered from the first inbound @-mention and
  cached in-process. Closes the "bot replies to its own
  ALL_MESSAGE_FORWARD echo" feedback loop.
- **Five-mode replyMode parity.** ``record`` (skip dispatch but log) and
  ``proactive`` (always dispatch with a "use NO_REPLY when unsure" prompt)
  are now first-class — no more silent fallback to ``mention-and-watch``.
- **``watch_regex``.** Account-level (and per-group) regex patterns that
  trigger group responses on content match. Configure via
  ``INFOFLOW_WATCH_REGEX`` (newline or ``|||`` separated; single ``|`` is
  preserved for regex alternation).
- **Follow-up window.** When ``INFOFLOW_FOLLOW_UP=true``, the bot stays
  engaged in a group for ``INFOFLOW_FOLLOW_UP_WINDOW`` seconds after its
  last reply — adopting OpenClaw's behavior for natural multi-turn chat.
- **Per-group config overrides.** Configure via ``INFOFLOW_GROUPS`` (JSON
  keyed by group id) — each entry can override ``reply_mode`` /
  ``watch_mentions`` / ``watch_regex`` / ``follow_up`` / ``follow_up_window``
  / ``system_prompt``.
- **Persistent sent-message store.** ``SentMessageStore`` now writes to a
  local SQLite file (default: ``~/.hermes/state/infoflow/sent-messages.db``)
  so cron sub-processes and adapter restarts can still recall messages.
  Auto-cleanup after 7 days. Override the state dir with
  ``HERMES_STATE_DIR``.
- **Recall correction.** When the LLM accidentally passes the inbound
  user-message id as the recall target, the delete path automatically:
  (1) swaps in the bot-message id the user quote-replied to (when there
  is one), or (2) drops to ``count=1`` if the inbound text is a clear
  "撤回上一条 / recall the last one" request. Unknown ids return error
  messages that include up to 5 recent bot-sent candidates so the LLM can
  self-correct on the next call.
- **Inbound-context registry** mirroring OpenClaw's ``inbound-context.ts``
  (TTL + max-size bounded; underpins recall correction).
- **body_for_agent format aligned** with OpenClaw — group @-mentions now
  render as ``@<name> (robotid:<N>) `` (was ``[at:@<name>]``).
- **InboundMessage** exposes ``discovered_robot_id``, ``fromid``, and
  ``event_type`` so the adapter can implement the above guards.

### Changed

- ``InfoflowAdapter.send()`` now reports failure if *any* chunk fails
  (mirrors OpenClaw's ``firstError`` semantics). Last successful message
  id is still surfaced for downstream recall.
- ``INFOFLOW_CONNECTION_MODE!='webhook'`` is now a hard error at
  ``connect()``-time instead of a silent fallback.
- ``text/plain`` inbound with an empty body now returns HTTP 400 (was 500),
  matching OpenClaw and Infoflow's retry-on-5xx policy.
- Group recall failures with unknown messageid surface up to 5 recent
  bot-sent candidates in the error text — gives the LLM something to
  self-correct from.
- ``SentMessageStore`` dedup set is now bounded by both TTL (5 min) and
  size (1000 entries by default — same cap as OpenClaw's
  ``DEDUP_MAX_SIZE``).
- Reply-target selection in ``MessageEvent.reply_to_message_id`` now
  prefers a bot-message target (when one is present in ``replyData``)
  rather than the first generic target.

### Removed

- ``INFOFLOW_CONNECTION_MODE`` removed from the setup wizard prompts —
  only ``webhook`` is supported and non-webhook values are now rejected
  at connect-time rather than silently downgraded.

### Changed (prior)

- Minimum Python version bumped from 3.10 to 3.11, matching hermes-agent's
  own ``requires-python = ">=3.11"``.
- CI matrix and PyPI classifiers extended to cover Python 3.13 and 3.14.
  hermes-infoflow already supported these in practice (the codebase runs
  cleanly on 3.14 locally); this change locks in regression coverage.

## [0.1.0] - 2026-05-01

### Added

- Initial Infoflow (如流) channel adapter for Hermes Agent.
- Webhook ingestion with AES-ECB decryption + echostr signature verification.
- Group and private (DM) message send via REST API (text / Markdown / link / image).
- @-mention helpers for groups (`at_all`, `mention_user_ids`).
- Message recall (撤回) for both private and group chats via `infoflow_recall_message` agent tool.
- Reply-mode policy: `ignore` / `mention-only` / `mention-and-watch`
  (`record` / `proactive` fall back to `mention-and-watch` with a warning).
- `watch_mentions` and `require_mention` group policies.
- Standalone (out-of-process) sender for cron `deliver=infoflow` jobs.
- Sibling PyPI package `hermes-infoflow-tools` with `hermes-infoflow-tools update`
  command (hybrid `--mode extract|pip`).
- Deploy scripts (`scripts/deploy.sh`, `scripts/lib/deploy-common.sh`)
  for local development.

### Known limitations

- Single-account only (no multi-account `accounts.*` subconfig yet).
- Webhook connection mode only — no WebSocket gateway.
- `count`-based recall across cron sub-processes requires SQLite sent-message store (enabled by default since 0.2.0); in-memory-only fallback is gateway-process-local.
