"""One-time interactive login → prints the TELEGRAM_SESSION string for .env.

    TELEGRAM_API_ID=... TELEGRAM_API_HASH=... python -m src.telegram_source.login

Get api_id/api_hash for YOUR OWN account at https://my.telegram.org (free).
The session string is a credential — treat it like a password, never commit it.
"""

from __future__ import annotations

import asyncio

from src.core.config import get_settings


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    cfg = get_settings()
    if not (cfg.telegram_api_id and cfg.telegram_api_hash):
        raise SystemExit("set TELEGRAM_API_ID and TELEGRAM_API_HASH first "
                         "(https://my.telegram.org)")
    client = TelegramClient(StringSession(), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.start()  # interactive: asks for phone + code (+2FA password)
    session = client.session.save()
    print("\nAdd this line to your .env (keep it secret!):\n")
    print(f"TELEGRAM_SESSION={session}\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
