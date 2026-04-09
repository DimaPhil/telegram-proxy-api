# telegram-proxy-api

Read-focused Telegram data proxy built with FastAPI and Telethon. It exposes structured JSON for chats, contacts, messages, search, and message context, plus raw media download endpoints for single files or all media attached to a message or album.

## Features

- FastAPI REST API with OpenAPI at `/openapi.json`, `/schema/openapi.json`, and `/schema/openapi.yaml`
- Long-lived Telethon session stored on a mounted volume for Docker and Ubuntu deployments
- Read-only v1 API for chats, contacts, self info, message retrieval, context, search, media manifests, streaming downloads, and ZIP bundles
- Telegram safety defaults: low `flood_sleep_threshold`, serialized history-heavy reads, cached entity resolution, and one-session-owner deployment guidance
- Local-only e2e harness for a personal Telegram account, plus unit/integration tests with 80% coverage enforcement

## Quick Start

1. Create Telegram API credentials at [my.telegram.org](https://my.telegram.org/apps).
2. Create a local env file:

```bash
cp .env.example .env
```

3. Authenticate once and persist the session volume:

```bash
docker compose run --rm --profile auth telegram-auth
```

4. Start the API:

```bash
docker compose up -d telegram-proxy
```

The API will be available at [http://localhost:8080](http://localhost:8080).

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

Run local real-account e2e tests:

```bash
export TELEGRAM_E2E_ENV_FILE=/absolute/path/to/.env
export E2E_CHAT_ID=123
export E2E_TEXT_MESSAGE_ID=456
export E2E_MEDIA_MESSAGE_ID=789
make test-e2e
```

Install the git hook:

```bash
make pre-commit-install
```

The pre-commit hook runs `pytest -m "not e2e"` and enforces the configured 80% coverage threshold.

## Ubuntu Deployment Notes

- Copy `.env` to the server and keep it out of version control.
- Run the auth profile once to create the persisted session volume.
- Keep the named Docker volume or bind-mount `/data/telegram` so restarts do not require re-authentication.
- Use `docker compose pull && docker compose up -d --build` for upgrades.
