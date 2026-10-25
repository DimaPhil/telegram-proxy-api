# Repository instructions

## Architecture

- This is a read-only personal Telegram API with an independently installable `tg` HTTP client in `cli/`. Keep CLI code free of server imports, Telethon, session files, Bot tokens and cache access. Shared contracts travel through the generated OpenAPI document.
- Keep the existing REST paths and user-account semantics. `TelegramGateway` selects providers; `TelethonGateway` owns MTProto. Bot messages are usable only through a verified Business/Secretary connection to the configured owner. History, search, context, albums and bundles remain Telethon operations.
- Never hide Bot authentication failures or rate limits through fallback. Never serve cached messages until polling has caught up. Commit a complete update batch and its cursor atomically. Preserve encryption, opaque indexes, retention, deletion handling and owner checks.
- Keep Bot mode off by default. Implementation/testing does not authorize connecting a real bot, enabling collection, deploying, migrating sessions or sending Telegram messages. Do not print credentials or token-bearing URLs.

## API and CLI changes

- Every endpoint must have an explicit stable `operation_id`, typed request/response schemas and accurate binary content declarations. Update API implementation, tests, CLI command exposure, bundled skill examples and documentation together.
- After a contract change run `python scripts/export_openapi.py`, inspect the generated diff, then `make check`. CI compares the complete normalized schema and verifies every operation has a CLI mapping. Never waive drift checks to land an API-only change.
- The canonical bundled skill is `cli/src/telegram_proxy_cli/skills/tg/SKILL.md`. Package it with the CLI. The release installer installs it globally using provenance markers; preserve unrelated skills and provide an explicit repair command.
- Prefer concrete typed components over a provider/plugin framework. Add tests for security, concurrency, durability, fallback and response parity. Use the existing pytest infrastructure; do not weaken the 80% API coverage gate.

## Validation and releases

- Use Python 3.12 or later in an isolated environment. `make check` runs formatting/lint, typing, API and CLI tests, and schema parity. Real-account tests are opt-in and never run in CI.
- Local, Docker, CI and release installs use `requirements.lock` as pip constraints. Regenerate intentionally with `uv pip compile pyproject.toml cli/pyproject.toml --extra dev --universal --output-file requirements.lock`, then run all gates. Do not hand-edit the generated lock or casually upgrade the schema generator.
- API and CLI release together from the same commit/version. Successful main CI triggers a GitHub release with the next minor version (`x.(y+1).0`); the release workflow also accepts a manual version. Do not commit automated version bumps or release from a feature branch.
- Release artifacts include both Python distributions, checksums and the CLI installer. Test built wheels and bundled resources, not only editable installs. Do not merge, publish or deploy unless the user authorizes it.
- Use one writer per area when delegating. Keep progress updates concise; report what passed and what remains unverified. Keep Markdown paragraphs on one physical line.
