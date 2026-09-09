"""Fire-and-forget Telegram push notifications - admin-only by design (see
docs/architecture.md's "Notifications" decision): a single TELEGRAM_CHAT_ID
in .env is the admin's own phone, and every Executor (whichever user it
serves) sends its alerts there, tagged with that user's own username, so
the admin gets one feed of "what's happening across every account" without
regular traders needing (or being able to configure) their own notification
channel. Never raises - a notification failure must not break trading logic.
"""
import logging
from pathlib import Path

import requests
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_DIR / "logs"

env = dotenv_values(PROJECT_DIR / ".env")
TELEGRAM_BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env.get("TELEGRAM_CHAT_ID", "")

_logger = logging.getLogger("notify")
_logger.setLevel(logging.ERROR)


def _log_error(exc: Exception):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "notify_errors.log", "a") as f:
        f.write(f"{exc}\n")


def notify(title: str, body: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"*{title}*\n{body}", "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 - fire-and-forget, never raise
        _log_error(exc)
