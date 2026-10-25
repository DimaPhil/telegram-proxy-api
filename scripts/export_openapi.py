"""Regenerate (or check) the CLI's complete, offline API contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from telegram_proxy_api.app import create_app
from telegram_proxy_api.config import Settings

TARGET = Path(__file__).resolve().parents[1] / "cli/src/telegram_proxy_cli/openapi.json"


def contract() -> str:
    schema = create_app(settings=Settings(_env_file=None)).openapi()  # type: ignore[call-arg]
    # Release versions change independently of the wire contract.
    schema["info"]["version"] = "0.0.0"
    schema["info"]["title"] = "Telegram Proxy API"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = contract()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != expected:
            parser.exit(1, "API/CLI contract drift: run python scripts/export_openapi.py and review CLI coverage.\n")
    else:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
