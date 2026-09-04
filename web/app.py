from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from finance_mcp.advanced import AdvancedFinanceDB
from finance_mcp.core import parse_simple_entry
from push_support import add_subscription, public_key as push_public_key, send_payload, subscription_count

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
STATIC_FILES = {
    "/manifest.webmanifest": (ROOT / "manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": (ROOT / "sw.js", "application/javascript; charset=utf-8"),
    "/pwa.js": (ROOT / "pwa.js", "application/javascript; charset=utf-8"),
    "/icon-192.png": (ROOT / "icon-192.png", "image/png"),
    "/icon-512.png": (ROOT / "icon-512.png", "image/png"),
    "/icon-maskable-512.png": (ROOT / "icon-maskable-512.png", "image/png"),
    "/app-icon-180.png": (ROOT / "app-icon-180.png", "image/png"),
    "/app-icon-192.png": (ROOT / "app-icon-192.png", "image/png"),
    "/app-icon-512.png": (ROOT / "app-icon-512.png", "image/png"),
    "/app-icon-maskable-512.png": (ROOT / "app-icon-maskable-512.png", "image/png"),
}
HOST = "127.0.0.1"
PORT = 13101


def db() -> AdvancedFinanceDB:
    return AdvancedFinanceDB()


def dashboard() -> dict:
    d = db()
    accounts = d.list_accounts()
    current = d.get_primary_account() or (accounts[0] if accounts else None)
    guards = d.safe_spending()
    return {
        "accounts": accounts,
        "current_account": current,
        "transactions": d.list_transactions(limit=20),
        "overview": d.financial_overview("this_month"),
        "budget_status": d.budget_status(),
        "goals": d.get_savings_goals(),
        "subscriptions": d.list_subscriptions(enabled_only=True),
        "recurring": d.get_recurring(enabled_only=False),
        "debts": d.list_debts(open_only=True),
        "guards": guards,
        "today": date.today().isoformat(),
        "version": "2.2.1",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FinanceWeb/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _read_json(self) -> dict:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0 or length > 16_384:
            raise ValueError("request body must be 1-16384 bytes")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self._json({"ok": True, "service": "finance-web", "core_version": "2.2.1"})
                return
            if path == "/api/push/config":
                self._json({"public_key": push_public_key(), "subscriptions": subscription_count()})
                return
            if path == "/api/push/status":
                self._json({"subscriptions": subscription_count()})
                return
            if path == "/api/dashboard":
                self._json(dashboard())
                return
            if path == "/api/transactions":
                self._json({"transactions": db().list_transactions(limit=100)})
                return
            if path in STATIC_FILES:
                file_path, content_type = STATIC_FILES[path]
                if not file_path.exists():
                    self._json({"error": "asset not found"}, HTTPStatus.NOT_FOUND)
                    return
                body = file_path.read_bytes()
                self._headers(HTTPStatus.OK, content_type, len(body))
                self.wfile.write(body)
                return
            if path in {"/", "/index.html"}:
                body = INDEX.read_bytes()
                self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
                self.wfile.write(body)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/push/subscribe":
                result = add_subscription(data.get("subscription") or data)
                self._json(result, HTTPStatus.CREATED)
                return
            if path == "/api/push/test":
                endpoint = str(data.get("endpoint", "")).strip() or None
                result = send_payload({
                    "title": "Finance Core",
                    "body": "ทดสอบแจ้งเตือนจาก Home Server สำเร็จ ✅",
                    "icon": "/app-icon-192.png?v=7",
                    "badge": "/app-icon-192.png?v=7",
                    "tag": "finance-remote-test",
                    "url": "/#overview",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }, endpoint=endpoint)
                status = HTTPStatus.OK if result.get("sent", 0) else HTTPStatus.BAD_GATEWAY
                self._json(result, status)
                return
            if path == "/api/entry":
                text = str(data.get("text", "")).strip()
                if not text:
                    raise ValueError("กรุณาใส่รายการ เช่น ข้าว 45")
                d = db()
                assessment = d.analyze_entry(text)
                confirm_ambiguous = bool(data.get("confirm_ambiguous", False))
                if assessment["needs_clarification"] and not confirm_ambiguous:
                    self._json({
                        "ok": False,
                        "needs_clarification": True,
                        "questions": assessment["questions"],
                        "preview": assessment["suggested"],
                        "error": "ขอรายละเอียดเพิ่ม: " + " / ".join(assessment["questions"]),
                    }, HTTPStatus.CONFLICT)
                    return
                parsed = parse_simple_entry(text)
                suggested = assessment["suggested"]
                result = d.add_transaction(
                    kind=suggested["kind"],
                    amount=parsed.amount,
                    description=parsed.description,
                    category=suggested["category"],
                    occurred_at=parsed.occurred_at,
                    raw_input=parsed.raw_input,
                    idempotency_key=str(data.get("idempotency_key", "")).strip() or None,
                    allow_duplicate=bool(data.get("allow_duplicate", False)),
                )
                self._json({"ok": True, "transaction": result, "dashboard": dashboard()}, HTTPStatus.CREATED)
                return
            if path == "/api/guard":
                name = str(data.get("name", "Spending Guard")).strip() or "Spending Guard"
                minimum = str(data.get("minimum_balance", "")).strip()
                deadline = str(data.get("deadline", "")).strip()
                account = data.get("account")
                if not minimum or not deadline:
                    raise ValueError("minimum_balance และ deadline จำเป็น")
                result = db().set_spending_guard(name, minimum, deadline, str(account) if account else None)
                self._json({"ok": True, "guard": result, "dashboard": dashboard()}, HTTPStatus.CREATED)
                return
            if path == "/api/budget":
                category = str(data.get("category", "")).strip()
                amount = str(data.get("amount", "")).strip()
                warning_percent = int(data.get("warning_percent", 80))
                if not category or not amount:
                    raise ValueError("category และ amount จำเป็น")
                result = db().set_budget(category, amount, warning_percent)
                self._json({"ok": True, "budget": result, "dashboard": dashboard()}, HTTPStatus.CREATED)
                return
            if path == "/api/goal":
                name = str(data.get("name", "")).strip()
                target = str(data.get("target", "")).strip()
                target_date = str(data.get("target_date", "")).strip() or None
                saved = str(data.get("saved", "")).strip() or None
                if not name or not target:
                    raise ValueError("name และ target จำเป็น")
                result = db().set_savings_goal(name, target, target_date, saved)
                self._json({"ok": True, "goal": result, "dashboard": dashboard()}, HTTPStatus.CREATED)
                return
            if path == "/api/recurring":
                name = str(data.get("name", "")).strip()
                kind = str(data.get("kind", "expense")).strip()
                amount = str(data.get("amount", "")).strip()
                description = str(data.get("description", "")).strip() or name
                category = str(data.get("category", "other")).strip() or "other"
                cadence = str(data.get("cadence", "monthly")).strip()
                next_due_date = str(data.get("next_due_date", "วันนี้")).strip() or "วันนี้"
                account = str(data.get("account", "")).strip() or None
                if not name or not amount:
                    raise ValueError("name และ amount จำเป็น")
                result = db().set_recurring(name, kind, amount, description, category, cadence, next_due_date, account)
                self._json({"ok": True, "recurring": result, "dashboard": dashboard()}, HTTPStatus.CREATED)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    if not INDEX.exists():
        raise SystemExit(f"missing {INDEX}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Finance Web listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
