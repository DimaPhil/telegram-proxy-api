# Maintainer context

This is the durable entry point for future repository sessions. Read root `AGENTS.md` first. This guide describes the implementation introduced in PR #1, not a running deployment. Check current source, Git history and GitHub before relying on historical delivery status. The original technical specification records the design and its earlier research; current code and tests win when details differ.

## Product and boundaries

The API exposes read-only access to one personal Telegram account. `tg` is a separate HTTP client for that deployed API. API and CLI are separate Python distributions, built and released from one repository at the same version. Installing `tg` requires HTTPX but no FastAPI, Telethon, Telegram session or Bot token. Python 3.12 is the minimum; CI tests 3.12 and 3.14.

There are three distinct credentials: the API bearer token used by `tg`; the personal-account Telethon session plus API credentials on the server; and the optional BotFather token on the server. The Bot cache encryption key is a fourth secret. Never interchange them, print them or send them to another service. CLI login does not authenticate Telegram, and CLI logout does not revoke the server token.

## Source map

| Concern | Canonical files |
| --- | --- |
| HTTP routes, lifespan, binary headers, OpenAPI | `src/telegram_proxy_api/app.py`, `dependencies.py`, `responses.py` |
| Public DTOs and configuration | `src/telegram_proxy_api/models.py`, `config.py` |
| Telethon implementation and hybrid facade | `src/telegram_proxy_api/gateway.py` (`TelethonGateway`, `TelegramGateway`, `GatewayResult`, `_ObservedBotStream`) |
| Bot HTTP transport, safe errors, file ownership | `src/telegram_proxy_api/bot_client.py` (`BotApiClient`, `BotApiError`, `BotFileStream`) |
| Encrypted SQLite and durable cursor | `src/telegram_proxy_api/bot_store.py` (`BotUpdateStore`) |
| Provider serialization | `serializers.py` for Telethon; `bot_serializers.py` for Bot payloads |
| CLI parser, catalog and transport | `cli/src/telegram_proxy_cli/main.py`, `catalog.py`, `client.py` |
| CLI credentials, skill and completion | `cli/src/telegram_proxy_cli/config.py`, `skill.py`, `completion.py` |
| Contract generator and snapshot | `scripts/export_openapi.py`, `cli/src/telegram_proxy_cli/openapi.json` |
| Consumer agent skill | `cli/src/telegram_proxy_cli/skills/tg/SKILL.md` and `source.json` |
| Release, installer and validation | `.github/workflows/{ci,release}.yml`, `scripts/{check,release,install-tg}.py`, `requirements.lock` |

## Request routing

FastAPI delegates to `TelegramGateway`. The facade returns `GatewayResult(data, meta)` for JSON operations. `meta.provider` and `meta.fallback_used` belong to each request; never add mutable “last provider” state. Binary responses carry `X-Telegram-Provider` and `X-Telegram-Fallback`. Health reports both providers and retains HTTP 200 even when degraded.

| Operation | Route in primary mode |
| --- | --- |
| Owner identity, contacts, dialogs | Telethon |
| History, search, message context | Telethon; the cache is never complete history |
| Resolve/chat metadata for negative IDs or usernames | Bot first when ready, but private results use Telethon |
| Positive private-user metadata | Telethon |
| Exact private message by numeric ID | Verified Business/Secretary cache, then permitted Telethon fallback |
| Single-message media manifest | Same cache route when `include_album=false` |
| Albums and ZIP bundles | Telethon |
| Single file with the cached message's `<message_id>:0` media ID | Bot preflight/stream if supported; hosted files known to exceed 20 MiB go directly to Telethon |

`off` starts only Telethon. `shadow` polls/stores Bot updates but routes public reads through Telethon. `primary` applies the table. Current code conservatively requires a verified active connection and caught-up poller even for Bot metadata reads. A Bot cache miss or recoverable transport/schema failure may use Telethon once. Unsupported capabilities select Telethon directly and are not marked as attempted Bot fallback. Bot authentication failure (503), rate limit (429) and identity mismatch must not be bypassed. Telethon-only endpoints remain independent of Bot failures.

## Data and concurrency invariants

- Standard bot-DM `message` updates are not the owner's private conversations and never enter the read cache. Only business updates from the configured owner and active connection qualify. Revalidate with `getBusinessConnection`; compare the owner to Telethon when available.
- A complete update batch, deletions and next offset commit in one SQLite transaction. A failed transaction must never advance acknowledgment. Durable ordering prevents stale replay from resurrecting deleted messages.
- A failed poll disables cached reads immediately. Drain pending updates before reenabling them. Connection edits/disable clear messages. A 24-hour polling gap clears stale messages. A week without nonempty updates resets the cursor/order epoch before polling, because Telegram may randomize update IDs; empty polls must not extend that epoch.
- Records use AES-256-GCM; separate encryption/index keys come from HKDF. Identifiers use opaque HMAC indices. The database is private (0600), retained messages expire after 30 days by default, and the root key lives separately. Key/owner mismatch or authentication failure must make the cache unusable.
- Recheck live cache/connection/error state after each awaited media preflight. A concurrent revoke, edit, deletion, 401 or 429 can invalidate a previously selected message. Close invalidated upstream streams. A midstream error aborts the transfer, updates provider health and never starts a fallback response after headers.
- `BotFileStream`, `_ObservedBotStream` and `ClosingStreamingResponse` cooperate to close upstream responses even before the first body byte. Preserve explicit `aclose` forwarding when refactoring.
- Authentication errors stay terminal until restart; concurrent recoverable errors cannot overwrite them. Cooldowns also survive concurrent failures. Check global error state before fallback, not merely the local exception.

## Changing the API or CLI

1. Change typed routes/DTOs and tests. Keep explicit operation IDs stable and declare binary response content accurately.
2. Run `.venv/bin/python scripts/export_openapi.py`. The bundled schema normalizes title/version so build version stamping does not cause false drift; the rest of the contract is compared in CI.
3. Review `catalog.py` command mappings and parser support. Flags derive from parameter schemas. Unsupported parameter/body schemas and duplicate operation IDs fail validation. Every operation needs a reviewed friendly command even though `operations call` is also available.
4. Update the bundled consumer skill, CLI README and completion tests when user-facing behavior changes. Completion derives candidates from the same parser without API calls or credential reads.
5. Run `make check` and inspect the generated diff. Do not hand-edit OpenAPI, weaken parity checks or import server code into the CLI.

## Validation

```bash
make install
make check
.venv/bin/python scripts/export_openapi.py --check
docker build --tag telegram-proxy-api:check .
```

`scripts/check.py` is the shared local/CI/release gate: Ruff, strict mypy, schema parity, server tests and CLI tests. Both suites enforce at least 80% coverage. Legacy Telethon serialization/session/auth modules have narrowly scoped typing exemptions; new modules remain strict. CI installs Bash/Zsh/Fish test shells. `tests/test_cli_api.py` exercises every API operation through the independent client. `test_hybrid_gateway.py`, `test_bot_store.py` and `test_bot_client.py` contain the security, ordering and concurrency regressions above. Release tests cover version selection, checksums, conflicts and installer failure behavior; `scripts/release.py` also smoke-tests an actual built CLI wheel in a temporary home.

CI validates Compose configuration then builds the Dockerfile with normal networking. Local Compose intentionally retains `build.network: host`; GitHub's builder rejects that privilege unless explicitly granted. Do not “repair” CI by weakening checks or changing local deployment networking unnecessarily.

`make test-e2e` is opt-in via `TELEGRAM_E2E_ENV_FILE` plus `E2E_CHAT_ID`, `E2E_TEXT_MESSAGE_ID` and `E2E_MEDIA_MESSAGE_ID`. The Bot test additionally requires `TELEGRAM_BOT_E2E=1`, `E2E_BOT_MESSAGE_ID` and configured primary mode. Do not run real-account tests against a session/token already owned by a running instance. Unit tests, builds and mocked transport success do not prove live account acceptance.

## Release and installation

Successful main-push CI triggers the Release workflow; pull-request CI does not publish. A manual UI run accepts a higher `X.Y.Z` version and is restricted to main. Automatic releases use the next minor version from existing semantic tags, falling back to the project baseline. Both project manifests are stamped only in the build checkout. The workflow serializes releases, checks ancestry, resumes interrupted drafts and makes an already-published commit a no-op. Artifacts include both wheels/sdists, `SHA256SUMS`, `release.json` and `install-tg.py`. No registry publication or server deployment is implied.

The supported installer verifies the CLI wheel checksum, creates a versioned isolated environment in `~/.local/share/tg/releases`, installs skill/completion and atomically updates `~/.local/bin/tg`. Previous environments remain available. It preserves unrelated executables, skills and completion files. Direct pip/wheel installs have no Python postinstall hook: users must run `tg skill install` and `tg completion install` themselves.

Managed skill copies live at `~/.agents/skills/tg`, with a relative Claude adapter at `~/.claude/skills/tg`. Completion files live under `~/.local/share/bash-completion/completions`, `~/.local/share/zsh/site-functions` and `~/.config/fish/completions`. Installation adds marked blocks to Bash/Zsh rc files; symlinked or conflicting managed files are preserved with warnings. For dotfile managers, use `tg completion install --no-shell-init` and integrate through the manager. Open a new shell afterward; Bash login profiles must source `.bashrc`.

## Operator checks and authorization

Keep Bot mode off until the owner approves the disclosed use under current Telegram terms. Configure token, owner ID and a separately stored encryption key; start shadow, connect the owner account, inspect `/healthz`, then deliberately enable primary. Roll back with off. If the store has no connection, reconnect the bot after polling begins. Existing webhooks require explicit operator removal; code never deletes them. Run one service process, one poller and one Telethon session owner. Protect any non-local deployment with API bearer authentication.

Before reporting “done,” distinguish local validation, hosted checks, merge, release publication, installation, deployment and live acceptance. Refresh the relevant evidence. No earlier implementation request authorizes a later merge, release dispatch, live activation, migration or destructive cache/session operation.
