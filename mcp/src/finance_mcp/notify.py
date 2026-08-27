from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import DISCORD_WEBHOOK, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "namkrub-finance-mcp/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def send_summary(text: str) -> list[str]:
    delivered: list[str] = []
    errors: list[str] = []
    if DISCORD_WEBHOOK:
        try:
            _post_json(DISCORD_WEBHOOK, {"content": text[:1900], "allowed_mentions": {"parse": []}})
            delivered.append("discord")
        except Exception as exc:
            errors.append(f"discord:{type(exc).__name__}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            body = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
            req = urllib.request.Request(url, data=body, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"HTTP {resp.status}")
            delivered.append("telegram")
        except Exception as exc:
            errors.append(f"telegram:{type(exc).__name__}")
    if errors and not delivered:
        raise RuntimeError("; ".join(errors))
    return delivered
