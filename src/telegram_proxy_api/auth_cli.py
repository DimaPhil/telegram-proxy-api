from __future__ import annotations

import asyncio
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import SQLiteSession

from .config import get_settings
from .session import normalize_string_session


async def _authenticate() -> None:
    settings = get_settings()
    settings.validate_telegram_credentials()
    try:
        settings.telegram_session_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"Cannot create Telegram session directory '{settings.telegram_session_dir}': {exc}"
        ) from exc

    if normalize_string_session(settings.telegram_session_string, settings.session_path):
        print(f"Stored session string in {settings.session_file_path}")
        return

    client = TelegramClient(
        SQLiteSession(str(settings.session_path)),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()
    if await client.is_user_authorized():
        print(f"Telegram session is already authorized: {settings.session_file_path}")
        await client.disconnect()
        return

    phone = input("Telegram phone number (international format): ").strip()
    await client.send_code_request(phone)
    code = input("Telegram login code: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = getpass("Telegram two-step verification password: ")
        await client.sign_in(password=password)

    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("Telegram authentication failed.")

    print(f"Telegram session saved to {settings.session_file_path}")
    await client.disconnect()


def main() -> None:
    asyncio.run(_authenticate())


if __name__ == "__main__":
    main()
