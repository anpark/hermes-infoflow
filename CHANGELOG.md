# Changelog

All notable changes to `hermes-infoflow` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/)
versioning (with prerelease suffixes such as `0.1.0b1` for betas).

## [Unreleased]

## [0.1.0] - TBD

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
- `count`-based recall is best-effort per gateway process (cross-process not supported).
