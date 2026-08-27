from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_NAME = os.getenv("FINANCE_TIMEZONE", "Asia/Bangkok")
TZ = ZoneInfo(TZ_NAME)
DB_PATH = Path(os.getenv("FINANCE_DB_PATH", str(Path.home() / ".local/share/finance-mcp/finance.sqlite3")))
REPORT_DIR = Path(os.getenv("FINANCE_REPORT_DIR", str(DB_PATH.parent / "reports")))
DEFAULT_CURRENCY = os.getenv("FINANCE_CURRENCY", "THB").upper()
DISCORD_WEBHOOK = os.getenv("FINANCE_DISCORD_WEBHOOK", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("FINANCE_TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("FINANCE_TELEGRAM_CHAT_ID", "").strip()
