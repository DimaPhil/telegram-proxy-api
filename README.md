# telegram-proxy-api

Read-only Telegram data proxy with FastAPI, Telethon and an optional official Bot API provider. The independently installed `tg` CLI calls the deployed HTTP API and provides JSON-first access for agents. Both packages release together.

See the [self-contained HTML walkthrough](.artifact-review/how-it-works.html) for architecture, routing, installation and releases. Future contributors should start with [AGENTS.md](AGENTS.md) and the [maintainer context](docs/AGENT_CONTEXT.md).

## Features

- FastAPI REST API with OpenAPI at `/openapi.json`, `/schema/openapi.json`, and `/schema/openapi.yaml`
- Long-lived Telethon session stored on a mounted volume for Docker and Ubuntu deployments
- Read-only v1 API for chats, contacts, self info, message retrieval, context, search, media manifests, streaming downloads, and ZIP bundles
- Telegram safety defaults: low `flood_sleep_threshold`, serialized history-heavy reads, cached entity resolution, and one-session-owner deployment guidance
- Local-only e2e harness for a personal Telegram account, plus unit/integration tests with 80% coverage enforcement
- Encrypted Business/Secretary update cache, verified owner identity, provider health and response provenance
- Schema-driven `tg` commands, a bundled agent skill, shell completion and enforced API/CLI parity

## Install `tg`

Requires Python 3.12+ on macOS or Linux. Download the installer from the [latest GitHub release](https://github.com/DimaPhil/telegram-proxy-api/releases/latest), then run it:

```bash
python3 install-tg.py
tg login --url https://your-api.example
tg auth-status --remote
tg chats list
tg messages search --query "meeting"
tg operations list
```

The installer verifies the CLI wheel checksum, installs into an isolated environment under `~/.local/share/tg`, and links `~/.local/bin/tg`. Put `~/.local/bin` on PATH. It installs the bundled skill globally and shell completions; unrelated existing files are preserved. Use `--version X.Y.Z` to install an exact release. `tg login` prompts for the API bearer token without echo; this is separate from Telegram account authentication. For automation, use `TG_API_URL` and `TG_API_TOKEN` or `--token-stdin`. Credentials are stored with mode 0600 and bound to one API URL. See [CLI documentation](cli/README.md).

## Provider routing and rollout

`TELEGRAM_BOT_MODE=off` preserves Telethon-only operation. `shadow` starts Bot polling and encryption but serves all public reads through Telethon. `primary` prefers Bot metadata and exact observed private messages, with capability-aware fallback. History, contacts, dialogs, search, context, albums and bundles always use Telethon; a cache miss is never proof that a message does not exist.

Before enabling collection, the owner must confirm that the disclosed proxy/cache use complies with the current [Telegram Bot Developer Terms](https://telegram.org/tos/bot-developers), including Business data restrictions. Keep mode `off` until that activation gate is met. Implementation and tests do not enable live collection.

1. Create a bot with BotFather, enable Secretary Mode and configure its privacy policy to disclose this private proxy and retention period.
2. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_OWNER_USER_ID` to the same account as Telethon, and `TELEGRAM_BOT_STORE_KEY` to a separately stored random base64-encoded 32-byte key. Never store the key with the SQLite database or commit either secret.
3. Start in `shadow`, connect the owner account to the bot and select eligible private chats. If no connection is known yet, disconnect/reconnect after polling starts so Telegram sends its connection update. Existing webhooks must be removed explicitly by the operator; startup never deletes them.
4. Check `/healthz`: both providers healthy, poller running and connection verified. Compare read responses, then switch to `primary`. Roll back with `off`.

Generate the cache key locally and transfer it directly to your secret manager:

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Messages expire after `TELEGRAM_BOT_RETENTION_DAYS` (default 30, range 1–365). Connection edits/disable and missed-update windows clear cached messages. Every record is authenticated/encrypted with AES-GCM and indexed with opaque HMAC keys. A failed poll disables cached reads until catch-up completes; 401 and 429 never fall back to evade authentication or cooldowns. Only one process may own the bot poller and Telethon session. Keep the cache on the persistent service volume. Key rotation means disabling Bot mode and deleting only the disposable Bot cache before starting with a new key; preserve the Telethon session.

JSON responses include `meta.provider` and `meta.fallback_used`; downloads include `X-Telegram-Provider` and `X-Telegram-Fallback`. Health retains its HTTP 200 response and reports degraded providers in its body. Enable `API_AUTH_ENABLED=true` with a strong token on any non-local deployment.

## Quick Start

1. Create Telegram API credentials at [my.telegram.org](https://my.telegram.org/apps).
2. Create a local env file:

```bash
cp .env.example .env
```

3. Authenticate once and persist the session volume:

```bash
docker compose --profile auth run --rm telegram-auth
```

4. Start the API:

```bash
docker compose up -d telegram-proxy
```

The API will be available at [http://localhost:4040](http://localhost:4040) by default. Set `PORT` to change it; the app also still accepts `APP_PORT` for compatibility.

## Manual Authentication Without Docker

```bash
make install
make auth
make run
```

The auth CLI stores the Telethon SQLite session under `TELEGRAM_SESSION_DIR/TELEGRAM_SESSION_NAME.session`.
For local development, `.env.example` uses `./data/telegram` so `make auth` writes to a project-local directory instead of a root-owned path.

## Important Session Rule

Only one long-lived service instance should own a Telegram session for a given account. Running the same session from multiple hosts or containers risks `AUTH_KEY_DUPLICATED`, which invalidates the session.

## API Surface

- `GET /healthz`
- `GET /me`
- `GET /contacts`
- `GET /resolve?value=@username`
- `GET /chats`
- `GET /chats/{chat_id}`
- `GET /chats/{chat_id}/messages`
- `GET /chats/{chat_id}/messages/{message_id}`
- `GET /chats/{chat_id}/messages/{message_id}/context`
- `GET /messages/search`
- `GET /chats/{chat_id}/messages/{message_id}/media`
- `GET /chats/{chat_id}/messages/{message_id}/media/{media_id}`
- `GET /chats/{chat_id}/messages/{message_id}/media/bundle`

## Media Download Semantics

- `GET /.../media` returns a manifest of downloadable media items.
- `include_album=true` collects sibling messages that share the same Telegram `grouped_id`.
- `GET /.../media/{media_id}` streams a single file with binary headers.
- `GET /.../media/bundle` returns a ZIP archive containing every file plus `manifest.json`.

## Development

Install dependencies:

```bash
make install
```

This creates a local `.venv` automatically and installs all dependencies there, so it works with Homebrew-managed Python environments that block system-wide `pip install`.

Run tests:

```bash
make test
```

Run the complete API/CLI contract, lint, format, strict typing and test gates:

```bash
make check
```

After changing an endpoint or schema, regenerate the bundled contract with `.venv/bin/python scripts/export_openapi.py`. Commit its diff together with command coverage, tests and skill/documentation changes. CI rejects stale schemas and uncovered API operations. Python 3.12 and 3.14 are tested.

`requirements.lock` supplies the same dependency constraints to local installs, Docker, CI and releases. To update dependencies intentionally, run `uv pip compile pyproject.toml cli/pyproject.toml --extra dev --universal --output-file requirements.lock`, reinstall, regenerate the schema if necessary and run the complete gate.

Run local real-account e2e tests:

```bash
export TELEGRAM_E2E_ENV_FILE=/absolute/path/to/.env
export E2E_CHAT_ID=123
export E2E_TEXT_MESSAGE_ID=456
export E2E_MEDIA_MESSAGE_ID=789
make test-e2e
```

For the Bot path, additionally set `TELEGRAM_BOT_E2E=1` and `E2E_BOT_MESSAGE_ID` to an already observed message in the selected private chat, using an explicitly configured `primary` environment. These tests never send messages. Stop the deployed instance before running local e2e against its session/token; never create a second session owner or poller.

Install the git hook:

```bash
make pre-commit-install
```

The pre-commit hook runs `scripts/check.py`, including the configured 80% API coverage threshold. Real-account tests remain opt-in.

## Releases

A successful CI run for a push to `main` triggers the Release workflow. It selects the next minor version from published/tagged versions (`x.(y+1).0`), builds the API and CLI from the same commit and publishes wheels, source distributions, checksums and the installer to GitHub Releases. The workflow also supports **Run workflow → version** for an explicit higher `X.Y.Z` version on `main`. Validation runs for automatic and manual releases. Version stamps are applied only to the build checkout, avoiding release commit loops. Re-running an already published commit is a no-op; interrupted draft releases can resume.

## Ubuntu Deployment Notes

- Copy `.env` to the server and keep it out of version control.
- Run the auth profile once to create the persisted session volume.
- Keep the named Docker volume or bind-mount `/data/telegram` so restarts do not require re-authentication.
- Use `docker compose pull && docker compose up -d --build` for upgrades.
