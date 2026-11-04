# CLI contributor context

Read root `AGENTS.md` and `docs/AGENT_CONTEXT.md`. This package is an HTTP-only consumer of the deployed API; it must remain installable without the server or its Telegram dependencies.

- `catalog.py` owns friendly command mappings and supported OpenAPI shapes; `main.py` constructs flags from that contract. All operations require coverage. Preserve JSON stdout, structured stderr and nonzero failures.
- Regenerate `src/telegram_proxy_cli/openapi.json` using the root generator. Never hand-edit it. Add typed request/body support and tests before accepting a newly unsupported schema.
- Credentials are URL-bound, privately stored and never followed through redirects. Do not add automatic retries, token output or provider bypass. Downloads require an explicit path and publish atomically; preserve existing files unless overwrite is requested.
- Completion must use the actual parser, remain offline and avoid credentials. Test Bash, Zsh and Fish behavior. Preserve unrelated completion files and shell configuration; support managed-dotfile installations.
- Keep the packaged consumer skill in `src/telegram_proxy_cli/skills/tg/` aligned with CLI examples. Provenance markers identify upgradable installs. Installer conflicts warn without overwriting unrelated user files.
- Run the root `make check`. For focused tests use `.venv/bin/python -m pytest -c cli/pyproject.toml cli/tests` from the repository root. Test wheel packaging and global skill/completion installation when changing resources or installation behavior.
- API and CLI versions release together. Do not independently bump or publish this package. The release installer handles automatic skill/completion setup; direct pip installs require the explicit setup commands.
