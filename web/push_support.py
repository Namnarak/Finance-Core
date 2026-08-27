from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pywebpush import WebPushException, webpush

ROOT = Path(__file__).resolve().parent
PRIVATE_KEY = ROOT / ".vapid-private.pem"
PUBLIC_KEY = ROOT / ".vapid-public.txt"
FINANCE_DB_PATH = Path(os.environ.get("FINANCE_DB_PATH", str(Path.home() / ".local/state/finance-mcp/finance.sqlite3")))
SUBSCRIPTIONS = FINANCE_DB_PATH.parent / "push-subscriptions.json"
VAPID_SUBJECT = os.environ.get("FINANCE_VAPID_SUBJECT", "mailto:admin@example.com")
_LOCK = threading.Lock()


def public_key() -> str:
    return PUBLIC_KEY.read_text(encoding="utf-8").strip()


def _load() -> list[dict[str, Any]]:
    if not SUBSCRIPTIONS.exists():
        return []
    try:
        value = json.loads(SUBSCRIPTIONS.read_text(encoding="utf-8"))
    except Exception:
        return []
    return value if isinstance(value, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    tmp = SUBSCRIPTIONS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(SUBSCRIPTIONS)
    SUBSCRIPTIONS.chmod(0o600)


def add_subscription(subscription: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(subscription.get("endpoint", "")).strip()
    keys = subscription.get("keys") or {}
    p256dh = str(keys.get("p256dh", "")).strip()
    auth = str(keys.get("auth", "")).strip()
    if not endpoint or not p256dh or not auth:
        raise ValueError("push subscription ไม่สมบูรณ์")

    normalized = {
        "endpoint": endpoint,
        "expirationTime": subscription.get("expirationTime"),
        "keys": {"p256dh": p256dh, "auth": auth},
    }

    with _LOCK:
        items = _load()
        replaced = False
        for idx, item in enumerate(items):
            if item.get("endpoint") == endpoint:
                items[idx] = normalized
                replaced = True
                break
        if not replaced:
            items.append(normalized)
        _save(items)
    return {"ok": True, "count": len(items), "replaced": replaced}


def remove_subscription(endpoint: str) -> int:
    endpoint = endpoint.strip()
    with _LOCK:
        items = _load()
        kept = [item for item in items if item.get("endpoint") != endpoint]
        if len(kept) != len(items):
            _save(kept)
        return len(kept)


def subscription_count() -> int:
    with _LOCK:
        return len(_load())


def send_payload(payload: dict[str, Any], endpoint: str | None = None) -> dict[str, Any]:
    with _LOCK:
        items = _load()

    targets = [item for item in items if endpoint is None or item.get("endpoint") == endpoint]
    sent = 0
    failed = 0
    stale: set[str] = set()
    errors: list[str] = []
    body = json.dumps(payload, ensure_ascii=False)

    for item in targets:
        try:
            webpush(
                subscription_info=item,
                data=body,
                vapid_private_key=str(PRIVATE_KEY),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=60,
            )
            sent += 1
        except WebPushException as exc:
            failed += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                stale.add(str(item.get("endpoint", "")))
            errors.append(f"{status or 'error'}: {str(exc)[:160]}")
        except Exception as exc:
            failed += 1
            errors.append(f"error: {str(exc)[:160]}")

    if stale:
        with _LOCK:
            current = _load()
            _save([item for item in current if item.get("endpoint") not in stale])

    return {
        "ok": sent > 0 and failed == 0,
        "targets": len(targets),
        "sent": sent,
        "failed": failed,
        "removed_stale": len(stale),
        "errors": errors[:5],
    }
