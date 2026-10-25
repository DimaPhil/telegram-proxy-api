# tg

An HTTP-only, JSON-first client for the deployed Telegram Proxy API. Install the GitHub release with the repository's release installer; it installs the bundled agent skill globally. API and CLI versions are released together.

```bash
tg login --url https://your-api.example
tg auth-status --remote
tg chats list
tg messages search --query "meeting"
tg operations list
tg operations describe OPERATION_ID
```

`tg login` reads the bearer token without echoing it, validates `/healthz`, and atomically saves credentials with mode 0600 under `$XDG_CONFIG_HOME/tg/credentials.json` (default `~/.config/tg/credentials.json`). For automation use `TG_API_URL` and `TG_API_TOKEN`, or `tg login --url URL --token-stdin`. Use `--anonymous` explicitly for an API with auth disabled. Remote APIs require HTTPS. Credentials saved for one URL are never reused for an overridden URL. HTTP redirects are never followed.

Commands return the API's JSON envelope on stdout. Errors are JSON on stderr and exit 1; interruption exits 130. `Retry-After` is exposed as `retry_after`; the CLI never retries automatically. `--help` and `--version` are human-readable. Binary operations require `--output FILE`; output is published only after the download completes, and overwrites require `--overwrite`.

The checked `src/telegram_proxy_cli/openapi.json` is generated from the server. `tg operations list|describe|call` exposes this contract, while friendly command flags derive from its parameter schemas. CI compares the generated schema with the bundled document and checks that every API operation has a reviewed friendly command. Never hand-edit the schema or add server imports to this package.

`tg skill install` safely installs the packaged skill into `~/.agents/skills/tg`, with a relative symlink at `~/.claude/skills/tg`. It updates only copies bearing this repository's provenance marker. The release installer uses `tg skill install --if-safe`, which reports unrelated conflicts without replacing them. Direct wheel/pip installs must run `tg skill install` explicitly because Python packaging has no standard postinstall hook.

For local development, run `make install` from the repository root, run the root contract generator, then run `.venv/bin/python -m pytest -c cli/pyproject.toml cli/tests`. This package depends only on HTTPX and the Python standard library. Both package test suites enforce 80% coverage.

## Shell completion

The release installer runs `tg completion install --if-safe`. Bash, Zsh, and Fish complete commands and options directly from the same parser/OpenAPI contract, including operation IDs and boolean values. Completion makes no network calls and reads no credentials.

`tg completion install` installs user completion files for all three shells. It adds clearly marked, idempotent integration blocks to `~/.bashrc` and `~/.zshrc`; existing content is preserved. Open a new shell after installing. Bash login shells must source `~/.bashrc` from their login profile, as with other Bash customizations. Fish uses its standard autoload directory. Zsh registers the function even when an existing completion cache predates the install.

For externally managed dotfiles, use `tg completion install --no-shell-init` and integrate `tg completion bash`, `tg completion zsh`, or `tg completion fish` through your configuration manager. Unmanaged completion files and symlinked shell configs are preserved with an actionable warning. Direct wheel/pip installs must run `tg completion install` explicitly; Python wheels have no standard postinstall hook.
