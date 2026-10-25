# Telegram Bot API + Telethon Integration Technical Specification

**Status:** Proposed

**Last verified:** 2026-08-19

**Target Telegram Bot API:** 10.2

**Repository:** `telegram-proxy-api`

## 1. Decision

Add Telegram's official HTTP Bot API as the primary provider only where it can preserve the existing REST API's semantics. Keep the current Telethon user session as the fallback and as the permanent provider for capabilities the Bot API does not expose.

The integration must use capability-based routing, not "call Bot API and catch every error." Unsupported Bot API calls are known before runtime and must go directly to Telethon.

The minimum useful implementation uses Telegram's 2026 **Secretary Mode** so the bot can receive live updates for selected private chats of the same account used by Telethon:

1. A small async HTTP Bot API client using `httpx`.
2. Long-polling for `business_*` updates into a small encrypted SQLite cache.
3. A `TelegramGateway` facade that routes each existing operation to Bot API/cache or Telethon using the matrix in section 6.
4. Provider health and response provenance.
5. Shadow mode for safe rollout and a one-setting rollback.

Do not replace Telethon. The current product is a personal-account historical read API, while the Bot API is primarily an update stream for a separate bot identity. Secretary Mode bridges selected private chats to the owner account, but still provides live updates rather than complete account history.

### Terminology correction

Telethon is an unofficial, third-party Python client library. The MTProto Telegram API it calls is an official Telegram API. The official HTTP Bot API is a narrower HTTP abstraction over Telegram's bot platform. See [Telethon's Bot API vs. MTProto explanation](https://docs.telethon.dev/en/stable/concepts/botapi-vs-mtproto.html).

## 2. Goals

- Prefer Telegram's official HTTP Bot API for chat metadata and messages/media already observed through a verified Secretary connection to the same personal account.
- Preserve every existing REST endpoint and its current user-account meaning.
- Use Telethon for full chat lists, contacts, historical reads, context, search, and safe misses.
- Make provider selection visible without breaking existing response bodies.
- Continue serving Bot-supported reads if the Telethon session is temporarily unavailable.
- Continue serving Telethon-supported reads if the Bot API is temporarily unavailable, except where fallback would hide a security/configuration error or evade rate limiting.
- Persist Bot updates before acknowledging them so restarts do not create gaps.
- Keep dependencies minimal: promote the already-used `httpx` to runtime and add only `cryptography` for required at-rest protection.

## 3. Non-goals

- Sending, editing, deleting, reacting to, or forwarding messages.
- Replacing the personal Telethon account with a bot identity.
- Making Bot API results appear to be a complete account history.
- Sending or other Telegram Business/Secretary write operations.
- Webhook deployment.
- A self-hosted local Bot API server.
- Multi-replica polling for one bot token.
- Full-text indexing or a new search engine.
- Migrating or deleting the existing Telethon session.
- Adding a Telegram bot framework such as `python-telegram-bot` or `aiogram`.

These are intentionally deferred. Add them only when a concrete requirement cannot be met by this design.

## 4. Current repository behavior

The current `TelegramGateway` is a single long-lived Telethon user client. FastAPI starts it during application lifespan and every endpoint calls it directly.

Current capabilities:

- User identity: `/me`
- User contacts: `/contacts`
- Entity resolution: `/resolve`
- Full user dialog list: `/chats`
- Chat metadata and historical messages
- Message context and account-wide/per-chat search
- Media manifests, downloads, album expansion, and ZIP bundles

Important implementation constraints already present:

- One process owns one Telethon SQLite session.
- History calls are concurrency-limited.
- Entity resolution is cached.
- The Docker volume at `/data/telegram` persists the Telethon session.
- Public API authentication exists but is disabled by default.
- Unit/integration coverage is enforced at 80%.

Baseline before this change: 46 non-e2e tests pass with 84.75% coverage.

## 5. Verified Bot API constraints

The source of truth is Telegram's [Bot API reference](https://core.telegram.org/bots/api) and [Bot FAQ](https://core.telegram.org/bots/faq).

As of Bot API 10.2:

- Authentication uses a BotFather token and HTTPS methods under `https://api.telegram.org/bot<TOKEN>/...`.
- Updates arrive through either `getUpdates` long polling or webhooks, never both.
- Telegram retains unconsumed updates for no longer than 24 hours.
- `getUpdates` returns at most 100 updates and confirms older updates when the next request supplies a higher offset.
- `getChat` returns current metadata for a chat accessible to the bot.
- The official method inventory has no general method to list dialogs, list contacts, fetch an arbitrary historical message, fetch chat history, get neighboring messages, or search account history.
- `getUserPersonalChatMessages` returns up to 20 posts from a user's profile-linked personal channel. It is not private-chat history and does not replace this service's message endpoints.
- Secretary Mode lets a user connect a bot to their account and select which private chats it can access. The bot receives `business_connection`, `business_message`, `edited_business_message`, and `deleted_business_messages` updates for that connection, except messages sent by itself and other bots.
- Secretary Mode is live/update-based. It does not add methods for old history, dialogs, contacts, context, or search.
- Standard bot visibility still depends on membership and privacy mode, but standard group/channel message ingestion is not used for reads in this plan because the Bot API does not provide general deletion updates for those messages.
- `getFile` downloads files up to 20 MB from Telegram's hosted Bot API. Its generated file URL is valid for at least one hour. A local Bot API server removes the download limit, but is not needed while Telethon is available for larger files.
- Bot API chat/user identifiers fit safely in signed 64-bit integers but should remain strings in this public API, matching current models.
- Telegram's Bot Developer Terms require purpose-limited use, deletion when data is no longer needed, and encryption at rest with the encryption key stored separately. Secretary/Business data has additional disclosure and use restrictions.

### Consequence

"Bot API primary" cannot mean "Bot API can implement every current endpoint." It means:

- Bot API is primary for operations it can answer completely.
- The durable encrypted Secretary update cache is primary for exact messages observed in selected private chats and their files.
- Telethon is selected immediately for known Bot API capability gaps.
- Partial Bot visibility is never presented as complete history.

## 6. Endpoint capability and routing matrix

| Existing endpoint | Bot API capability | Final route in `primary` mode | Reason |
|---|---|---|---|
| `GET /healthz` | `getMe`, polling status | Hybrid local status | Report both providers and the poller. |
| `GET /me` | `getMe` returns the bot, not the personal account | Telethon only | Preserve current user-account semantics. |
| `GET /contacts` | No contact-list method | Telethon only | Received contact cards are not an account contact list. |
| `GET /resolve` | `getChat` supports accessible chat IDs and public channel/supergroup usernames | Bot first for negative IDs and `@username`; Telethon fallback | Positive private-user IDs go directly to Telethon. |
| `GET /chats` | No dialog-list method | Telethon only | Cached observed chats are incomplete. |
| `GET /chats/{chat_id}` | `getChat` | Bot first for groups/channels; Telethon fallback | Metadata can be represented by the existing model. |
| `GET /chats/{chat_id}/messages` | No history method | Telethon only | The update cache can contain privacy and downtime gaps. |
| `GET /chats/{chat_id}/messages/{message_id}` | No arbitrary get-message method | Verified Secretary cache first for selected private chats; Telethon fallback. All other chats use Telethon. | A Secretary cache hit represents the same personal-account chat; a miss is not proof of absence. |
| `GET /.../{message_id}/context` | No neighboring-message/history method | Telethon only | Context must not contain silent gaps. |
| `GET /messages/search` | No history-search method | Telethon only | No partial cache search in v1. |
| `GET /.../{message_id}/media` with `include_album=false` | Cached Secretary message contains `file_id` | Verified Secretary cache first; Telethon fallback | One exact personal-account message can be represented safely. |
| `GET /.../{message_id}/media` with `include_album=true` | Update cache may contain an incomplete album | Telethon only | Album completeness cannot be proven from updates. |
| `GET /.../media/{media_id}` | `getFile` for cached Secretary message `file_id` | Bot first; Telethon fallback for the same verified personal-account message | Preserve streaming response headers. |
| `GET /.../media/bundle` | Cache may be incomplete; hosted Bot API has a 20 MB/file limit | Telethon only | Preserve complete album and bundle semantics. |

### Identity safety rule

There are two kinds of private Bot API messages and they must never be confused:

- A normal `message` update is a conversation between a user and the bot. It is **not** the personal account's conversation and must never enter the REST read cache.
- A `business_message` update belongs to a private chat managed through a Secretary/Business connection. It may enter the REST read cache only after the connection owner is verified as the same Telegram user authenticated by Telethon.

Require one configured `TELEGRAM_BOT_OWNER_USER_ID`. At startup and on each `business_connection` update, verify `BusinessConnection.user.id` against that value. When Telethon is healthy, also verify it against `TelethonGateway.get_me().id`. A mismatch disables Bot-backed reads and makes health degraded; it must never fall back into a mixed cache.

Only messages carrying the active verified `business_connection_id` may be returned as personal-account data. Standard `message`/`edited_message` updates are ignored by the store.

## 7. Proposed architecture

```text
FastAPI endpoint
      |
      v
TelegramGateway facade
      |
      +-- static capability/identity decision
      |       |
      |       +-- BotApiClient ------> official HTTP Bot API
      |       |          |
      |       |          +--> BotUpdateStore (encrypted records in SQLite)
      |       |
      |       +-- TelethonGateway ---> Telegram MTProto API
      |
      +-- existing Pydantic response models + provider metadata
```

Use three concrete components. Do not add an abstract provider framework.

### 7.1 `BotApiClient`

A small async `httpx.AsyncClient` wrapper responsible only for HTTP transport and Bot API envelope handling.

Required methods:

```python
async def start() -> dict: ...                 # validates token with getMe
async def close() -> None: ...
async def get_me() -> dict: ...
async def get_chat(chat_ref: str) -> dict: ...
async def get_updates(offset: int | None, timeout: int) -> list[dict]: ...
async def get_webhook_info() -> dict: ...
async def get_business_connection(connection_id: str) -> dict: ...
async def get_file(file_id: str) -> dict: ...
async def stream_file(file_path: str) -> AsyncIterator[bytes]: ...
```

One internal `call(method, payload)` method must:

1. POST JSON to the method endpoint.
2. Parse the `{ok, result, error_code, description, parameters}` envelope.
3. Return `result` when `ok` is true.
4. Raise typed local errors when `ok` is false, even if HTTP status is 200.
5. Never include the bot token or full request URL in logs/errors.

Use the configured request timeout for normal calls. For `getUpdates`, use a read timeout of `poll_timeout + 5` seconds.

### 7.2 `BotUpdateStore`

Use Python's built-in `sqlite3`; do not add an ORM. Encrypt all Telegram-derived records with AES-256-GCM before writing them. Derive separate encryption and deterministic-index keys from `TELEGRAM_BOT_STORE_KEY` with HKDF-SHA256. Use HMAC-SHA256 opaque keys for lookups so chat IDs, message IDs, owner IDs, and connection IDs are not stored in plaintext.

Default database path: `/data/telegram/bot-cache.sqlite3`, already covered by the service's persistent Docker volume.

The root key must be a random base64-encoded 32-byte secret supplied by the deployment secret manager. It must never be written beside the database. Generate it with:

```bash
python -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Add the `cryptography` runtime dependency for AES-GCM and HKDF; do not invent encryption with standard-library primitives.

Derive 64 bytes with HKDF-SHA256 using salt `telegram-proxy-bot-store-v1` and info `record-and-index-keys`; use the first 32 bytes as the AES-GCM key and the remaining 32 as the HMAC key. Define opaque keys as `HMAC-SHA256(index_key, kind + NUL + canonical decimal/string identifier)`. Store each encrypted record as `12-byte random nonce || AESGCM.encrypt(nonce, canonical_json, aad)`, with its table name and opaque primary key as associated data. Any authentication failure makes Bot health degraded and the record unusable; never return partially decoded data.

Minimal schema:

```sql
CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE bot_connections (
    connection_key BLOB PRIMARY KEY,
    owner_key BLOB NOT NULL,
    update_id INTEGER NOT NULL,
    is_enabled INTEGER NOT NULL,
    encrypted_json BLOB NOT NULL
);

CREATE TABLE bot_messages (
    connection_key BLOB NOT NULL,
    chat_key BLOB NOT NULL,
    message_key BLOB NOT NULL,
    update_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    encrypted_json BLOB NOT NULL,
    PRIMARY KEY (connection_key, chat_key, message_key)
);

CREATE INDEX bot_messages_expiry ON bot_messages(expires_at);
```

Store only encrypted raw Bot API JSON plus opaque keys and update ordering. Do not normalize senders, files, or text into additional tables; serializers can decrypt and read the JSON. Album lookups are unnecessary because albums always route to Telethon.

Required operations:

```python
def initialize() -> None: ...
def next_update_offset() -> int | None: ...
def ingest_updates(updates: list[dict]) -> None: ...
def get_active_connection() -> dict | None: ...
def get_connection(connection_id: str) -> dict | None: ...
def get_message(connection_id: str, chat_id: int, message_id: int) -> dict | None: ...
```

`ingest_updates` must use one transaction for the full batch:

1. For `business_connection`, verify the owner ID, then upsert the encrypted connection only when the incoming `update_id` is not older than the stored one. Conservatively clear cached messages for that connection when an existing connection is edited or disabled because the Bot API does not enumerate which selected chats changed.
2. For `business_message` or `edited_business_message`, accept only the active verified connection and upsert the encrypted message only when the incoming `update_id` is not older than the stored one. Set `expires_at` from local ingestion time plus the configured retention period.
3. For `deleted_business_messages`, accept only the active verified connection and delete the listed opaque message keys.
4. Ignore standard `message`, `edited_message`, and channel/group updates for REST storage.
5. Delete expired message rows.
6. Store `next_update_offset = max(update_id) + 1` in `bot_state`.
7. Commit.

Support exactly one active connection for the configured owner. When a newer valid connection replaces it, clear messages for the previous connection and mark only the new one active. Ignore and do not persist every connection owned by a different user.

The next `getUpdates` call acknowledges the committed batch. If the process crashes before that call, Telegram may redeliver updates, but the primary keys and `update_id` condition make ingestion idempotent.

Do not advance an in-memory offset when the SQLite transaction fails.

Persist `last_poll_success_at` in `bot_state`. If the service starts after 24 hours or more without a successful poll, clear cached messages before enabling Bot-backed reads because Telegram may have dropped edit/deletion updates. Keep the verified connection record and fetch its current state again with `getBusinessConnection`. This sacrifices cache hits rather than serving potentially deleted or no-longer-authorized data.

### 7.3 `TelegramGateway` facade

Keep the existing public class name `TelegramGateway` for FastAPI and tests. Rename the current implementation class in the same module to `TelethonGateway`; it retains the current code without behavior changes.

The new facade owns:

- One `BotApiClient` when Bot mode is `shadow` or `primary`.
- One `BotUpdateStore` when Bot mode is `shadow` or `primary`.
- One active, verified Secretary/Business connection for the configured owner.
- One `TelethonGateway` whenever Telethon credentials/session are available.
- One background long-poll task.
- The static routing rules from section 6.

Do not introduce a base class, provider registry, plugin system, or dependency-injection framework. Constructor injection of the three concrete components is sufficient for tests.

Use one small result wrapper so provider metadata is request-local and concurrency-safe:

```python
@dataclass(slots=True)
class GatewayResult(Generic[T]):
    data: T
    meta: ResponseMeta
```

Facade JSON methods return `GatewayResult`; the existing `TelethonGateway` method signatures stay unchanged and the facade adapts their current values/tuples. Add `provider` and `fallback_used` fields directly to `MediaStream` and `MediaBundle` so FastAPI can set the binary response headers. Do not store "last provider" on the gateway because concurrent requests would race.

## 8. Configuration contract

Add these settings:

| Environment variable | Type/default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_MODE` | `off` | `off`, `shadow`, or `primary`. |
| `TELEGRAM_BOT_TOKEN` | empty secret | BotFather token; required in `shadow` and `primary`. |
| `TELEGRAM_BOT_OWNER_USER_ID` | empty integer | Telegram user ID of the personal account; required in `shadow` and `primary`. |
| `TELEGRAM_BOT_STORE_KEY` | empty secret | Base64-encoded random 32-byte key for the Bot cache; required in `shadow` and `primary`. |
| `TELEGRAM_BOT_API_BASE_URL` | `https://api.telegram.org` | Allows a future local Bot API server without code changes. |
| `TELEGRAM_BOT_REQUEST_TIMEOUT` | `10` | Normal Bot API request timeout in seconds. |
| `TELEGRAM_BOT_POLL_TIMEOUT` | `30` | `getUpdates` long-poll timeout in seconds. |
| `TELEGRAM_BOT_STORE_PATH` | `/data/telegram/bot-cache.sqlite3` | Durable update/cache database. |
| `TELEGRAM_BOT_RETENTION_DAYS` | `30` | Days to retain encrypted Bot-observed messages before Telethon becomes the only source. |

Use Pydantic `SecretStr` for the token and store key.

Mode behavior:

- `off`: current Telethon-only behavior; no Bot HTTP calls or polling.
- `shadow`: validate and poll the bot, persist verified Secretary updates, but route all public reads exactly as today through Telethon.
- `primary`: use the routing matrix in section 6.

The list of requested updates is a code constant, not configuration:

```python
[
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]
```

Extra update types can be added when a feature consumes them.

Validation rules:

- Reject startup configuration when mode is `shadow` or `primary` and the token, owner user ID, or store key is empty/invalid. Require retention days to be between 1 and 365.
- Validate the token once with `getMe` at startup.
- Require Secretary Mode to be enabled in BotFather and one connection to the configured owner account. The service may poll while waiting for the initial connection, but Bot-backed reads remain disabled and health remains degraded.
- Verify every connection with `getBusinessConnection` before enabling Bot-backed reads.
- The Bot API has no method to list all business connections. If the encrypted store has no active connection record, the operator must disconnect/reconnect the bot after polling starts so the service receives a fresh `business_connection` update.
- Call `getWebhookInfo` at startup. If a webhook URL is configured, fail Bot startup with an actionable error; do not automatically delete the webhook or drop pending updates.
- A failed Telethon startup must not kill the process when Bot startup succeeded. Mark health degraded and return a capability-unavailable 503 for Telethon-only endpoints.
- A failed Bot startup in `primary` mode marks health degraded. Telethon-supported reads remain available, but the failure must be obvious in `/healthz` and logs.

## 9. Polling lifecycle

Long polling is the default because this service already has a single-instance deployment constraint and no public HTTPS ingress requirement.

Startup order:

1. Initialize SQLite schema.
2. Start and validate `BotApiClient` when mode is not `off`.
3. Inspect webhook status.
4. Start Telethon independently and load the personal account ID when available.
5. Load/revalidate the active Secretary connection and compare its owner to configuration and Telethon.
6. Drain and persist pending updates with short polling until Telegram returns an empty batch; keep Bot-backed reads disabled during catch-up.
7. Start one long-poll task when Bot startup is healthy and caught up.
8. Mark application ready after initialization completes.

Poll loop:

```text
load durable offset
  -> getUpdates(offset, limit=100, timeout=30)
  -> ingest whole batch and durable next offset in one transaction
  -> repeat with new durable offset
```

On network/5xx errors, retry the poll loop with a capped exponential delay of 1, 2, 4, 8, 16, then 30 seconds. Reset the delay after a successful response. On Bot API 429, sleep exactly `retry_after` seconds. On 401, stop the poller and mark Bot health unhealthy until restart/configuration repair.

Any poll failure immediately disables Bot-cache reads until the poller successfully drains all pending updates again. This prevents serving a message that may have been edited, deleted, or removed from the connection while the update stream was unhealthy. During network/5xx poll failures, cache-eligible reads may fall back to Telethon. During a Bot 429 cooldown they return 429, and after Bot 401 they return the Bot-auth 503; this preserves the no-rate-limit/auth-bypass rules. Telethon-only endpoints continue normally.

Shutdown order:

1. Cancel and await the poll task.
2. Close the Bot HTTP client.
3. Stop Telethon.
4. Close SQLite.

## 10. Serialization and public contract

Keep all existing endpoint paths and Pydantic data models.

### Bot chat mapping

Map `getChat`/cached `Chat` fields as follows:

| Existing field | Bot API source |
|---|---|
| `id` | decimal string of `chat.id` |
| `title` | `title`, else joined `first_name`/`last_name`, else `username`, else `Unknown` |
| `username` | `username` |
| `type` | Bot API `type`; map `group`, `supergroup`, `channel`, `private` directly |
| `participant_count` | `null` in v1; do not make an extra `getChatMemberCount` call |
| `about` | `description`, then `bio` |
| `unread_count` | `0`; Bot API does not expose it |
| `archived` | `false`; Bot API does not expose it |
| `telegram_raw` | complete sanitized Bot API object |

### Bot message mapping

- Accept only `business_message`/`edited_business_message` belonging to the active verified connection.
- Keep `business_connection_id` only in the encrypted store record; remove it from the public `telegram_raw` copy and do not add it to the public model.
- `id`: `message_id` as string.
- `chat_id`: `chat.id` as string.
- `date`/`edit_date`: UTC datetimes from Unix timestamps.
- `text`: `text`, else `caption`, else `null`.
- `sender`: prefer `from`; otherwise use `sender_chat`.
- `is_forwarded`: whether `forward_origin` exists.
- `forward_info`: map origin date and user/chat/channel/hidden-user fields into the existing optional fields; preserve the full origin in `telegram_raw`.
- `reply_to_message_id`: nested `reply_to_message.message_id` when present.
- `grouped_id`: `media_group_id`.
- `views` and `forwards`: `null` unless present in the Bot API object.
- `telegram_raw`: sanitized Bot API message with `business_connection_id` removed.

For media, choose one downloadable object per message using this priority:

1. `document`
2. `video`
3. `animation`
4. `audio`
5. `voice`
6. `video_note`
7. `sticker`
8. largest `photo` item by `file_size`, then dimensions

Use the selected object's `file_id`, `file_unique_id`, size, dimensions, duration, filename, and MIME type. Preserve the existing public `media_id` format `<message_id>:0` and existing `access_path` format.

### Provider provenance

Extend `ResponseMeta` with optional fields:

```python
provider: Literal["bot_api", "telethon"] | None = None
fallback_used: bool | None = None
```

Set them on every single-provider Telegram data response. `/healthz` reports both providers in its body and leaves these fields null. Existing clients ignore new fields; no path or `data` schema changes.

Binary/ZIP endpoints do not have a JSON `meta` object. Set `X-Telegram-Provider: bot_api|telethon` and `X-Telegram-Fallback: true|false` response headers on those endpoints instead.

Do not expose raw fallback exception text in API responses.

## 11. Fallback policy

Fallback is permitted only for idempotent read operations explicitly listed in section 6.

| Bot outcome | Telethon fallback? | Required behavior |
|---|---|---|
| Capability is absent from Bot API | Yes, selected before calling Bot | Do not make a pointless Bot request. |
| Exact message is absent from the verified Secretary cache | Yes | Cache miss is not a 404. |
| Cached file reports `file_size > 20 MB` on hosted Bot API | Yes, selected before `getFile` | Route directly to Telethon; do not make a known-impossible Bot request. |
| `getFile` returns Telegram's file-too-large error | Yes | Use Telethon once; do not classify unrelated 400 responses as file-size fallback. |
| Bot returns chat not found or bot lacks access | Yes for `resolve`/chat metadata | Return Telethon result with `fallback_used=true`; log reason category. |
| Bot HTTP timeout or 5xx | Yes for whitelisted read | Mark Bot degraded and use Telethon once. |
| Invalid Bot response/schema | Yes for whitelisted read | Log as provider failure; do not silently ignore health impact. |
| Bot 401 | No | Token is invalid/revoked; expose provider unhealthy. |
| Bot 429 with `retry_after` | No | Return 429 with `Retry-After`; do not use Telethon to evade Bot rate limits. |
| Poller is catching up after startup/network recovery | No Bot-cache read; Telethon fallback is allowed | Cache contents are not current until the queue is drained; 401/429 still follow the stricter rows above. |
| Invalid caller input | No | Preserve 400 response. |
| Telethon also fails | No second retry chain | Return the most useful typed problem response. |

Never infer completeness from an empty Bot cache result. Never serve partial history/search/context from the cache. Never serve normal bot-DM updates as personal-account data.

## 12. Error mapping

Add local Bot errors that subclass `TelegramProxyError` or are translated by the facade:

- Invalid/revoked token: 503, `telegram-bot-auth-required`.
- Provider unavailable/timeout: 502, `telegram-bot-unavailable` unless Telethon succeeds.
- Rate limited: 429, existing flood-wait shape with `Retry-After` from `parameters.retry_after`.
- Chat/resource unavailable after both providers: 404.
- Required provider capability unhealthy: 503, `telegram-capability-unavailable`.
- Invalid Telegram envelope/JSON: 502, `telegram-bot-invalid-response`.

If Telegram returns `parameters.migrate_to_chat_id`, retry `getChat` once using the migrated ID and cache that mapping for the process lifetime. Do not retry any other 400 automatically.

## 13. Health and observability

Keep `GET /healthz` and HTTP 200 behavior, but extend `HealthStatus`:

```json
{
  "status": "ok | degraded",
  "telegram_connected": true,
  "session_file": "/data/telegram/telegram_proxy.session",
  "primary_provider": "bot_api | telethon",
  "bot_api": {
    "mode": "off | shadow | primary",
    "healthy": true,
    "poller_running": true,
    "secretary_connection_verified": true,
    "last_update_at": "2026-08-19T12:00:00Z",
    "last_error": null
  },
  "telethon": {
    "healthy": true
  }
}
```

Backward compatibility:

- Keep `telegram_connected`; set it true if at least one provider is healthy.
- Keep `session_file`; it continues to describe only the Telethon session.
- In `primary` mode, status is `ok` only when both Bot API and Telethon are healthy because most current endpoints still require Telethon.
- In `shadow` mode, Bot failure makes status `degraded` but cannot change endpoint responses.

Use standard-library logging; add no metrics dependency. Log one structured event for:

- Bot startup success/failure.
- Poller stopped/retrying and retry delay.
- Each provider fallback with method and reason category.
- Bot cache batch count and resulting offset at debug level.

Never log:

- Bot token or token-bearing URL.
- Telethon session strings/auth keys.
- Raw message text, media URLs, contact data, or full Telegram JSON.

## 14. Security and privacy requirements

### Mandatory activation/compliance gate

Before enabling `shadow` or `primary` in any environment containing real messages, the owner must confirm that this service's exact use of Secretary data complies with the then-current [Telegram Bot Platform Developer Terms](https://telegram.org/tos/bot-developers), especially:

- Section 4.3's ban on collecting more data than essential and on scraping/large-dataset creation.
- Section 5.2(e)'s restriction on external services that diverge significantly from intended Bot Platform uses.
- Section 5.4's requirement that Business/Secretary message data be used only to provide the disclosed chatbot service and not disclosed to third parties without authorization.

This repository is an external read proxy, not a conventional reply bot, so compatibility with those intended-use restrictions must not be assumed. This specification is technical guidance, not a legal determination. If the gate is not affirmatively passed, keep `TELEGRAM_BOT_MODE=off`; the existing Telethon implementation remains the supported path and no Secretary data may be collected.

- Keep `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_STORE_KEY` in `.env`/deployment secrets only; `.env` is already gitignored. In production, supply them through a secret manager rather than committing/copying them with the database.
- Treat token exposure as compromise and revoke it through BotFather before redeploying.
- Treat store-key exposure as compromise. Rotate by disabling Bot mode, deleting only the disposable Bot cache, generating a new key, and returning through shadow mode; the Telethon session is unaffected.
- Restrict the SQLite file and Telethon session volume to the service user. All Telegram-derived cache payloads and identifiers must be encrypted or HMAC-indexed as specified in section 7.2.
- Run production with `API_AUTH_ENABLED=true` and a strong bearer token if the service is reachable outside localhost/private networking.
- Do not expose Bot API file URLs to callers because they contain the bot token. Proxy file bytes through this service exactly as current media endpoints do.
- This implementation is a single-owner private bot. Ignore and do not persist connections whose owner does not match `TELEGRAM_BOT_OWNER_USER_ID`.
- Configure the bot's privacy policy in BotFather and accurately describe the local proxy/cache and its default 30-day retention. Telegram requires deletion when data is no longer needed or access ends; expiry, connection disable/edit, and missed-update handling must clear cached messages as described above.
- Use Secretary data only to provide this proxy. Do not send message contents/files to third-party APIs without the account owner's explicit authorization.
- Do not automatically disable privacy mode, add the bot to chats, promote it, delete webhooks, or drop pending updates from application code. Those are explicit operator actions.

## 15. File-by-file implementation plan

### Step 1: dependency and settings

Files:

- `pyproject.toml`
- `src/telegram_proxy_api/config.py`
- `.env.example`

Actions:

1. Move `httpx>=0.28.1` from dev-only dependencies to runtime dependencies; `respx` remains dev-only.
2. Add `cryptography` as a runtime dependency for AES-GCM/HKDF cache protection.
3. Add the nine settings from section 8.
4. Use `Literal["off", "shadow", "primary"]` and `SecretStr`.
5. Add validation tests for token/key/owner-required modes and defaults.

Acceptance:

- Existing Telethon-only configuration still starts unchanged with mode `off`.
- The bot token and store key never appear in settings `repr`.

### Step 2: Bot API transport

Files:

- New `src/telegram_proxy_api/bot_api.py`
- New `tests/test_bot_api.py`

Actions:

1. Implement the client methods and envelope parsing from section 7.1, including `getBusinessConnection`.
2. Implement typed errors and `retry_after` extraction.
3. Stream files without buffering the whole response.
4. Use `respx` to test success, Bot API errors, HTTP errors, timeouts, invalid JSON, 429, and token redaction.

Acceptance:

- No third-party bot framework is added.
- All request URLs are built in one private method and never logged.

### Step 3: durable update cache and poller

Files:

- New `src/telegram_proxy_api/bot_store.py`
- New `tests/test_bot_store.py`

Actions:

1. Create the schema and encryption/key-derivation helpers from section 7.2.
2. Implement owner/connection verification and atomic, idempotent business-update ingestion.
3. Implement exact verified-connection message lookup, deletion handling, and expiry cleanup during startup/ingestion.
4. Implement the lifecycle-owned poll task, continuity clearing, and retry rules.
5. Refuse to poll when a webhook is configured.

Acceptance:

- Restarting from a committed offset does not duplicate or regress an edited message.
- A failed SQLite transaction does not advance the offset.
- A connection edit/disable and a 24-hour polling gap clear cached messages before Bot reads resume.
- The SQLite file does not contain plaintext owner, connection, chat, message, sender, text, or media identifiers/content.

### Step 4: Bot serializers

Files:

- `src/telegram_proxy_api/serializers.py`
- `tests/test_serializers.py`

Actions:

1. Add dict-based Bot chat/message/media serializers using the mappings in section 10.
2. Reuse current Pydantic models, `safe_telegram_dict`, filename handling, and access-path conventions after decrypting verified Secretary records.
3. Test text, captions, forwards, replies, albums, documents, and largest-photo selection.

Acceptance:

- Existing response `data` schemas do not change.
- Bot and Telethon versions of the same managed private-chat message use the same public chat/message/media IDs.

### Step 5: hybrid gateway

Files:

- `src/telegram_proxy_api/gateway.py`
- `src/telegram_proxy_api/dependencies.py`
- New `tests/test_hybrid_gateway.py`
- Existing gateway/app tests

Actions:

1. Rename the current implementation to `TelethonGateway` without changing its behavior.
2. Add the new `TelegramGateway` facade in the same file.
3. Implement owner/connection verification and the exact route and fallback tables from sections 6 and 11.
4. Start providers independently and own the poll task.
5. Return provider provenance with gateway results.
6. Keep constructor injection for fake Bot/Telethon/store objects.

Acceptance:

- No endpoint calls Bot API for a known unsupported capability.
- Normal bot-DM updates never read from or merge with personal-account data; verified Secretary messages may serve the matching private chat.
- A Bot cache miss falls back instead of returning false 404.
- Bot 429 and 401 are not hidden by fallback.

### Step 6: API, health, docs, and operations

Files:

- `src/telegram_proxy_api/models.py`
- `src/telegram_proxy_api/app.py`
- `README.md`
- `.env.example`
- `tests/test_app.py`
- `tests/e2e/test_personal_telegram.py`

Actions:

1. Add provider metadata and provider health models.
2. Preserve every current route and data model.
3. Document BotFather creation, Secretary Mode, connecting the same account, selected-chat access, privacy/terms requirements, and rollout modes.
4. Add optional local Bot e2e environment variables; never run them in CI.
5. Document the single-poller constraint beside the existing single-Telethon-session rule.

Acceptance:

- `/openapi.json` contains all existing paths.
- Existing Telethon e2e test still works with Bot mode `off`.
- New Bot e2e tests are opt-in and skip cleanly without secrets.

## 16. Test plan

### Unit tests

- Bot API response envelope and typed error mapping.
- Token redaction.
- Store-key redaction, AES-GCM authentication failure, and opaque HMAC lookup keys.
- Secretary connection owner verification and mismatch rejection.
- Chat/message/media serialization.
- SQLite schema initialization and migration from an empty file.
- Idempotent duplicate update ingestion.
- Later edit wins; older update cannot overwrite it.
- Offset advances only in the successful transaction.
- Business deletion removes the cached message; connection edit/disable clears connection messages.
- Expired cached messages are deleted and fall back to Telethon.
- A 24-hour polling gap clears stale cache rows before reads resume.
- Provider routing for every row in section 6.
- Fallback policy for every row in section 11.
- Standard bot-DM versus verified Secretary-message identity guard.
- Health status for both healthy, Bot-only healthy, Telethon-only healthy, and both unhealthy.

### Integration tests

- FastAPI with fake Bot/Telethon providers preserves existing JSON shapes.
- Secretary-cached exact private-chat message and media stream return `provider=bot_api`.
- Cache miss returns Telethon data with `fallback_used=true`.
- History/search/context never call Bot.
- Bot file response is streamed and never exposes a token-bearing redirect or URL.
- Bot 429 returns `Retry-After`.

### Optional real-account e2e

Use a dedicated private bot, the same owner account as Telethon, and one controlled private chat selected in Secretary Mode. Required operator setup:

1. Create the bot through BotFather.
2. Enable Secretary Mode in BotFather.
3. Start the service in shadow mode so the poller is waiting for connection updates.
4. Connect the same account used by the Telethon session and grant the bot access only to the controlled private chat.
5. Send one incoming text message, one document under 20 MB, and one media album from the other party after the poller is running.
6. Edit and delete one test message to verify cache mutation.
7. Run opt-in tests using `TELEGRAM_BOT_E2E_TOKEN`, `TELEGRAM_BOT_OWNER_USER_ID`, `TELEGRAM_BOT_STORE_KEY`, `E2E_BOT_CHAT_ID`, and known message IDs.

Do not use an ordinary chat with the bot for cross-provider comparison. Only a verified Secretary `business_message` represents the same personal-account private chat as Telethon.

### Required validation commands

```bash
make test
docker compose build
```

The final implementation must retain at least 80% coverage and pass all existing non-e2e tests.

## 17. Rollout plan

### Phase 0: merge disabled

1. Deploy code with `TELEGRAM_BOT_MODE=off`.
2. Verify all existing endpoints, OpenAPI, Telethon e2e, and health match current behavior.
3. Confirm no Bot API network calls occur.

Gate: existing behavior is unchanged.

### Phase 1: shadow ingestion

1. Pass and record the activation/compliance gate from section 14. Stop here and remain `off` if it is not passed.
2. Create a dedicated bot and store its token in deployment secrets.
3. Generate and securely store a 32-byte Bot cache key; verify the cache volume and key are stored separately.
4. Enable Secretary Mode in BotFather.
5. While still in Bot mode `off`, read the current account ID from `GET /me` and use `data.id` as `TELEGRAM_BOT_OWNER_USER_ID`.
6. Set `TELEGRAM_BOT_OWNER_USER_ID`, `TELEGRAM_BOT_STORE_KEY`, `TELEGRAM_BOT_MODE=shadow`, and restart.
7. Verify `getMe`, empty webhook status, and that the poller is healthy while waiting for a connection.
8. Connect the same Telegram account used by Telethon and initially select one controlled private chat. If it was connected earlier, disconnect/reconnect it now to emit a fresh update.
9. Verify owner/connection match, durable offset movement, encrypted cache contents, restart recovery, edit/delete handling, expiry behavior, and cache growth.
10. Confirm every public endpoint still reports `provider=telethon`.

Gate: 24 hours without poller errors, update gaps, token leakage, or endpoint behavior changes.

### Phase 2: primary routing in one controlled chat

1. Set `TELEGRAM_BOT_MODE=primary`.
2. Verify one supported public group/channel chat-detail lookup uses `getChat`; verify an exact Secretary message and a sub-20-MB cached file use Bot cache/API.
3. Verify an older uncached message, list, context, search, album, and bundle use Telethon.
4. Stop/block Bot API access temporarily and verify allowed reads fall back to Telethon.
5. Stop Telethon temporarily and verify Bot-supported reads remain available while Telethon-only reads return typed 503 responses.

Gate: all section 18 acceptance criteria pass.

### Phase 3: broaden selected-chat access

Add only the additional private chats the owner wants the Secretary bot to process. The resulting connection edit must clear the old cache; new updates repopulate it. No code change is required.

## 18. Final acceptance criteria

The integration is complete only when all are true:

1. Existing routes and `data` response schemas remain backward compatible.
2. Bot API 10.2 token validation and long polling work without a bot SDK.
3. Verified Secretary updates and next offset commit atomically and resume after restart.
4. No message is acknowledged before durable persistence.
5. Exact cached messages and single files from the active owner-verified Secretary connection use Bot API/cache in primary mode.
6. Chat metadata uses Bot API first for supported chat references.
7. Contacts, dialog lists, history, context, search, albums, and bundles route directly to Telethon.
8. Ordinary bot private chats never mix with personal-account conversations; only owner-verified Secretary messages can serve private-chat reads.
9. Cache misses fall back to Telethon rather than returning false 404s.
10. Bot 401 and 429 are never hidden by Telethon fallback.
11. Every single-provider Telegram JSON response identifies its provider/fallback in `meta`; binary and ZIP responses identify them in `X-Telegram-Provider` and `X-Telegram-Fallback` headers; health reports both providers in its body.
12. Health reports Bot, poller, and Telethon state independently.
13. Bot tokens, store keys, decrypted cache data, and token-bearing file URLs never appear in logs, API payloads, or exceptions beyond the existing authorized Telegram response fields.
14. `TELEGRAM_BOT_MODE=off` is a complete runtime rollback with no database deletion.
15. All tests and Docker build pass with at least 80% coverage.

## 19. Rollback

Fast rollback:

1. Set `TELEGRAM_BOT_MODE=off`.
2. Restart the service.
3. Verify `/healthz` reports Telethon as primary and no poller.
4. Verify one historical message and one media download.

The Bot SQLite cache may remain on disk; it is inert in `off` mode and preserves the option to resume. Do not delete it during an incident.

If the token may be compromised, revoke it through BotFather, replace the deployment secret, and only then return to `shadow` mode.

No Telethon session migration or rollback is required because this plan never mutates or replaces the existing session.

## 20. Deferred upgrades and their triggers

| Deferred item | Add only when |
|---|---|
| Webhooks | Multiple application replicas or ingress-driven delivery is a real requirement. |
| Local Bot API server | Bot-primary downloads over 20 MB must work without Telethon. |
| Secretary/Business write operations | The read-only product explicitly adds sending, editing, deletion, or read-receipt features. |
| Standard bot group/channel update reads | Deletion/current-state semantics can be preserved without verifying each hit through Telethon. |
| Full-text Bot cache search | Users explicitly request bot-observed-only search and cache size makes SQL scanning too slow. |
| Custom retention scheduler | Startup/ingestion cleanup is insufficient for a measured storage or compliance requirement. |
| Multi-provider abstraction/registry | A third Telegram provider is actually being added. |
| Multi-replica poll ownership | The service is deployed with more than one active replica for the same bot token. |

## 21. Source references

- [Telegram Bot API reference and changelog](https://core.telegram.org/bots/api)
- [Getting updates and 24-hour retention](https://core.telegram.org/bots/api#getting-updates)
- [`getUpdates` offset semantics](https://core.telegram.org/bots/api#getupdates)
- [`getChat`](https://core.telegram.org/bots/api#getchat)
- [`getFile` and the hosted 20 MB download limit](https://core.telegram.org/bots/api#getfile)
- [Local Bot API server capabilities](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
- [Bot privacy mode](https://core.telegram.org/bots/features#privacy-mode)
- [Secretary Bots](https://core.telegram.org/bots/features#secretary-bots)
- [Business/Secretary update types](https://core.telegram.org/bots/api#update)
- [Bot Developer Terms: privacy, retention, security, and Telegram Business](https://telegram.org/tos/bot-developers)
- [Telegram Bots FAQ](https://core.telegram.org/bots/faq)
- [Telethon: HTTP Bot API vs. MTProto](https://docs.telethon.dev/en/stable/concepts/botapi-vs-mtproto.html)
