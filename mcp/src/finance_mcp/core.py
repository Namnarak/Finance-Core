from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator, Literal

from .config import DB_PATH, DEFAULT_CURRENCY, TZ

Kind = Literal["income", "expense"]

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL DEFAULT 'THB',
    category TEXT NOT NULL DEFAULT 'other',
    description TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payment_method TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'mcp',
    raw_input TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_occurred_at
ON transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_transactions_kind
ON transactions(kind);
CREATE INDEX IF NOT EXISTS idx_transactions_category
ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_transactions_active_date
ON transactions(deleted_at, occurred_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    actor TEXT NOT NULL DEFAULT 'mcp',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_runs (
    summary_date TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    delivered_to TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    period TEXT NOT NULL DEFAULT 'monthly' CHECK(period IN ('monthly')),
    warning_percent INTEGER NOT NULL DEFAULT 80 CHECK(warning_percent BETWEEN 1 AND 100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_goals (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    target_minor INTEGER NOT NULL CHECK(target_minor > 0),
    saved_minor INTEGER NOT NULL DEFAULT 0 CHECK(saved_minor >= 0),
    target_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

@dataclass(slots=True)
class ParsedEntry:
    kind: Kind
    amount: str
    description: str
    category: str
    occurred_at: str
    raw_input: str


def now_local() -> datetime:
    return datetime.now(TZ)


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def money_to_minor(value: str | int | float | Decimal) -> int:
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"จำนวนเงินไม่ถูกต้อง: {value!r}") from exc
    if d <= 0:
        raise ValueError("จำนวนเงินต้องมากกว่า 0")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def minor_to_money(value: int) -> str:
    return f"{Decimal(value) / Decimal(100):,.2f}"


def normalize_datetime(value: str | None) -> str:
    if not value:
        return iso_now()
    raw = value.strip()
    low = raw.lower()
    now = now_local()
    if raw in {"วันนี้", "today"}:
        return now.isoformat(timespec="seconds")
    if raw in {"เมื่อวาน", "yesterday"}:
        return (now - timedelta(days=1)).isoformat(timespec="seconds")

    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw)
    if m:
        day, month, year = map(int, m.groups())
        if year < 100:
            # Thai short years such as 69 commonly mean B.E. 2569 (= 2026).
            # 00-59 are treated as Gregorian 20xx; 60-99 as B.E. 25xx.
            year = year + 1957 if year >= 60 else year + 2000
        if year >= 2400:
            year -= 543
        dt = datetime(year, month, day, now.hour, now.minute, now.second, tzinfo=TZ)
        return dt.isoformat(timespec="seconds")

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        else:
            dt = dt.astimezone(TZ)
        return dt.isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError("วันที่ไม่ถูกต้อง ใช้ ISO, วันนี้, เมื่อวาน หรือ DD/MM/YYYY") from exc


def normalize_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raw = value.strip()
    if raw in {"วันนี้", "today"}:
        return now_local().date().isoformat()
    if raw in {"เมื่อวาน", "yesterday"}:
        return (now_local().date() - timedelta(days=1)).isoformat()
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw)
    if m:
        d, mo, y = map(int, m.groups())
        if y < 100:
            y = y + 1957 if y >= 60 else y + 2000
        if y >= 2400:
            y -= 543
        return date(y, mo, d).isoformat()
    return date.fromisoformat(raw).isoformat()


def _category_for(text: str, kind: Kind) -> str:
    t = text.lower()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("food", ("ข้าว", "อาหาร", "กาแฟ", "ชา", "น้ำ", "กิน", "ร้านอาหาร", "grabfood", "lineman", "ขนม", "ขนมปัง", "ข้าวเกรียบ", "โออิชิ", "เครื่องดื่ม", "นม")),
        ("entertainment", ("vtuber", "วีทูบเบอร์", "วีทูบ", "โดเนท", "donate", "superchat", "super chat", "ซุปเปอร์แชท")),
        ("transport", ("น้ำมัน", "แท็กซี่", "taxi", "grab", "bts", "mrt", "รถ", "ทางด่วน", "เดินทาง")),
        ("server_hosting", ("vps", "server", "เซิร์ฟ", "hosting", "host", "cloudflare", "bare metal", "colocation")),
        ("ai", ("openai", "chatgpt", "claude", "gemini", "deepseek", "qwen", "copilot", "token", "api")),
        ("domain", ("domain", "โดเมน", ".com", ".xyz", ".site", ".me")),
        ("software", ("license", "subscription", "สมาชิก", "software", "saas")),
        ("utilities", ("ค่าไฟ", "ค่าน้ำ", "เน็ต", "internet", "โทรศัพท์", "มือถือ")),
        ("shopping", ("ซื้อ", "ของ", "shopee", "lazada")),
        ("education", ("โรงเรียน", "เรียน", "หนังสือ", "ค่าเทอม")),
    ]
    for category, words in rules:
        if any(w in t for w in words):
            return category
    if kind == "income":
        if any(w in t for w in ("งาน", "ค่าจ้าง", "ลูกค้า", "freelance", "ทำเว็บ", "ทำบอท", "ปลั๊กอิน")):
            return "work_income"
        if any(w in t for w in ("ขาย", "ยอดขาย")):
            return "sales_income"
        return "income_other"
    return "other"


def parse_simple_entry(text: str) -> ParsedEntry:
    raw = " ".join(text.strip().split())
    if not raw:
        raise ValueError("ข้อความว่าง")

    # Prefer an amount followed by บาท/฿, otherwise use the last number in the input.
    money_match = re.search(r"([0-9][0-9,]*(?:\.\d{1,2})?)\s*(?:บาท|฿)", raw, re.I)
    if money_match is None:
        nums = list(re.finditer(r"(?<![\w/.-])([0-9][0-9,]*(?:\.\d{1,2})?)(?![\w/.-])", raw))
        if not nums:
            raise ValueError("หา 'จำนวนเงิน' ไม่เจอ เช่น 'ข้าว 75' หรือ 'รับค่าทำเว็บ 1500'")
        money_match = nums[-1]
    amount = money_match.group(1)
    money_to_minor(amount)  # validate

    income_words = ("รายรับ", "รับเงิน", "ได้เงิน", "โอนเข้า", "ค่าจ้าง", "ยอดขาย", "ขายได้", "ลูกค้าจ่าย", "รับค่า")
    expense_words = ("รายจ่าย", "จ่าย", "ซื้อ", "ค่า", "เติม", "เสีย", "หัก", "โอนออก")
    low = raw.lower()
    if any(w in low for w in income_words):
        kind: Kind = "income"
    elif any(w in low for w in expense_words):
        kind = "expense"
    else:
        # Short entries like "ข้าว 75" are overwhelmingly expense entries.
        kind = "expense"

    occurred = iso_now()
    if "เมื่อวาน" in raw:
        occurred = (now_local() - timedelta(days=1)).isoformat(timespec="seconds")
    else:
        dm = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", raw)
        if dm:
            occurred = normalize_datetime(dm.group(1))

    description = raw
    description = description[: money_match.start()] + description[money_match.end() :]
    description = re.sub(r"\b(?:วันนี้|เมื่อวาน|today|yesterday)\b", "", description, flags=re.I)
    description = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", description)
    description = re.sub(r"\s+", " ", description).strip(" -–—:,.")
    if not description:
        description = "รายการทั่วไป"

    return ParsedEntry(
        kind=kind,
        amount=amount.replace(",", ""),
        description=description,
        category=_category_for(raw, kind),
        occurred_at=occurred,
        raw_input=raw,
    )


def assess_parsed_entry(p: ParsedEntry, *, learned_from_history: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assess whether a parsed short entry is safe to write without asking the user again.

    This intentionally prefers clarification over silent guessing. History can raise confidence
    for specific descriptions, but merchant-only/generic descriptions still require details.
    """
    raw = p.raw_input.strip()
    low = raw.lower()
    desc = p.description.strip()
    desc_low = desc.lower()
    amount = p.amount

    income_words = ("รายรับ", "รับเงิน", "ได้เงิน", "โอนเข้า", "ค่าจ้าง", "ยอดขาย", "ขายได้", "ลูกค้าจ่าย", "รับค่า")
    expense_words = ("รายจ่าย", "จ่าย", "ซื้อ", "ค่า", "เติม", "เสีย", "หัก", "โอนออก", "โดเนท", "donate")
    explicit_kind = any(w in low for w in income_words + expense_words)

    generic = {
        "รายการทั่วไป", "รายการ", "เงิน", "ของ", "ซื้อของ", "ค่า", "จ่าย", "รับ",
        "payment", "pay", "expense", "income", "shop", "store", "ร้าน",
    }
    merchant_only = {
        "สหกรณ์", "เซเว่น", "7-11", "7eleven", "7-eleven", "shopee", "lazada",
        "โลตัส", "lotus", "big c", "bigc", "tops",
    }

    reasons: list[str] = []
    questions: list[str] = []

    if desc_low in generic or len(re.sub(r"[^\wก-๙]", "", desc_low)) < 2:
        reasons.append("description_too_generic")
        questions.append(f"{amount} บาทนี้เป็นค่าอะไรหรือซื้ออะไร?")

    if desc_low in merchant_only:
        reasons.append("merchant_without_item_detail")
        questions.append(f"ที่ {desc} {amount} บาท ซื้ออะไรหรือเป็นค่าอะไร?")

    learned = learned_from_history or None
    learned_category = learned.get("category") if learned else None
    learned_kind = learned.get("kind") if learned else None

    suggested_category = learned_category or p.category
    suggested_kind = learned_kind or p.kind

    # If no explicit direction and no strong category/history signal, don't silently assume expense.
    strong_kind_signal = explicit_kind or bool(learned_kind) or suggested_category not in {"other", "income_other"} or desc_low in merchant_only
    if not strong_kind_signal:
        reasons.append("kind_inferred_without_signal")
        questions.append("รายการนี้เป็นเงินเข้า หรือเงินออก?")

    # 'other' is allowed only when the description itself is specific enough and the kind is explicit.
    if suggested_category in {"other", "income_other"} and not learned_category and desc_low not in generic and desc_low not in merchant_only:
        reasons.append("category_unknown")
        questions.append(f"“{desc}” คือรายการเกี่ยวกับอะไร? จะได้จัดหมวดให้ถูก")

    # De-duplicate questions while preserving order.
    questions = list(dict.fromkeys(questions))
    needs = bool(questions)
    confidence = "low" if len(reasons) >= 2 else ("medium" if needs else ("high" if explicit_kind or learned else "medium"))

    return {
        "needs_clarification": needs,
        "confidence": confidence,
        "reasons": reasons,
        "questions": questions,
        "suggested": {
            "kind": suggested_kind,
            "amount": amount,
            "description": desc,
            "category": suggested_category,
            "occurred_at": p.occurred_at,
        },
        "learned_from_history": learned,
        "raw_input": raw,
    }


class FinanceDB:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.executescript(SCHEMA)

    def _audit(self, conn: sqlite3.Connection, txid: str | None, action: str, before: Any, after: Any, actor: str) -> None:
        conn.execute(
            "INSERT INTO audit_log(transaction_id,action,before_json,after_json,actor,created_at) VALUES(?,?,?,?,?,?)",
            (
                txid,
                action,
                json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
                json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
                actor,
                iso_now(),
            ),
        )

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        d["amount"] = minor_to_money(int(d.pop("amount_minor")))
        return d

    def add_transaction(
        self,
        *,
        kind: Kind,
        amount: str | int | float | Decimal,
        description: str,
        category: str = "other",
        occurred_at: str | None = None,
        payment_method: str | None = None,
        note: str | None = None,
        source: str = "mcp",
        raw_input: str | None = None,
        idempotency_key: str | None = None,
        allow_duplicate: bool = False,
        actor: str = "mcp",
    ) -> dict[str, Any]:
        if kind not in {"income", "expense"}:
            raise ValueError("kind ต้องเป็น income หรือ expense")
        desc = description.strip()
        if not desc:
            raise ValueError("description ห้ามว่าง")
        minor = money_to_minor(amount)
        occurred = normalize_datetime(occurred_at)
        category = (category or "other").strip().lower().replace(" ", "_")[:64]
        currency = DEFAULT_CURRENCY
        created = iso_now()
        txid = str(uuid.uuid4())

        with self.connect() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM transactions WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing:
                    out = self._public(existing)
                    out["duplicate_prevented"] = True
                    out["message"] = "รายการนี้เคยถูกบันทึกแล้ว (idempotency key เดิม)"
                    return out

            if not allow_duplicate:
                cutoff = (now_local() - timedelta(seconds=90)).isoformat(timespec="seconds")
                recent = conn.execute(
                    """
                    SELECT * FROM transactions
                    WHERE deleted_at IS NULL AND kind=? AND amount_minor=?
                      AND lower(description)=lower(?)
                      AND created_at >= ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (kind, minor, desc, cutoff),
                ).fetchone()
                if recent:
                    out = self._public(recent)
                    out["duplicate_prevented"] = True
                    out["message"] = "พบรายการเหมือนกันที่เพิ่งบันทึก จึงไม่เพิ่มซ้ำ ถ้าตั้งใจให้ซ้ำให้ใช้ allow_duplicate=true"
                    return out

            conn.execute(
                """
                INSERT INTO transactions(
                    id,kind,amount_minor,currency,category,description,occurred_at,
                    payment_method,note,source,raw_input,idempotency_key,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    txid, kind, minor, currency, category, desc, occurred,
                    payment_method, note, source, raw_input, idempotency_key, created, created,
                ),
            )
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
            out = self._public(row)
            self._audit(conn, txid, "create", None, out, actor)
            out["duplicate_prevented"] = False
            return out

    def get_transaction(self, transaction_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM transactions WHERE id=?"
        args: list[Any] = [transaction_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return self._public(row) if row else None

    def update_transaction(self, transaction_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"kind", "amount", "description", "category", "occurred_at", "payment_method", "note"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"ฟิลด์ที่แก้ไม่ได้: {', '.join(sorted(unknown))}")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted_at IS NULL", (transaction_id,)).fetchone()
            if not row:
                raise ValueError("ไม่พบรายการ")
            before = self._public(row)
            sets: list[str] = []
            vals: list[Any] = []
            for key, value in changes.items():
                if value is None:
                    continue
                if key == "amount":
                    sets.append("amount_minor=?")
                    vals.append(money_to_minor(value))
                elif key == "occurred_at":
                    sets.append("occurred_at=?")
                    vals.append(normalize_datetime(str(value)))
                elif key == "kind":
                    if value not in {"income", "expense"}:
                        raise ValueError("kind ต้องเป็น income หรือ expense")
                    sets.append("kind=?")
                    vals.append(value)
                else:
                    sets.append(f"{key}=?")
                    vals.append(str(value).strip())
            if not sets:
                return before
            sets.append("updated_at=?")
            vals.append(iso_now())
            vals.append(transaction_id)
            conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", vals)
            after_row = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            after = self._public(after_row)
            self._audit(conn, transaction_id, "update", before, after, "mcp")
            return after

    def delete_transaction(self, transaction_id: str, reason: str = "user requested") -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted_at IS NULL", (transaction_id,)).fetchone()
            if not row:
                raise ValueError("ไม่พบรายการ หรือถูกลบไปแล้ว")
            before = self._public(row)
            deleted_at = iso_now()
            conn.execute("UPDATE transactions SET deleted_at=?, updated_at=? WHERE id=?", (deleted_at, deleted_at, transaction_id))
            after = dict(before)
            after["deleted_at"] = deleted_at
            after["delete_reason"] = reason
            self._audit(conn, transaction_id, "delete", before, after, "mcp")
            return after

    def undo_last(self, reason: str = "undo last transaction") -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM transactions WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise ValueError("ยังไม่มีรายการให้ย้อนกลับ")
        return self.delete_transaction(str(row["id"]), reason)

    def analyze_entry(self, text: str) -> dict[str, Any]:
        p = parse_simple_entry(text)
        learned: dict[str, Any] | None = None
        desc = p.description.strip()
        # Learn from history only when deterministic rules do not already know the category.
        # This avoids over-broad matches such as "ข้าว" borrowing metadata from "ข้าวเกรียบ ...".
        if desc and desc != "รายการทั่วไป" and p.category in {"other", "income_other"}:
            with self.connect() as conn:
                # Prefer exact description; then a conservative contains-match for specific text.
                row = conn.execute(
                    "SELECT kind,category,description,occurred_at FROM transactions "
                    "WHERE deleted_at IS NULL AND lower(description)=lower(?) "
                    "ORDER BY occurred_at DESC, created_at DESC LIMIT 1",
                    (desc,),
                ).fetchone()
                if row is None and len(desc) >= 4:
                    row = conn.execute(
                        "SELECT kind,category,description,occurred_at FROM transactions "
                        "WHERE deleted_at IS NULL AND (lower(description) LIKE lower(?) OR lower(?) LIKE '%' || lower(description) || '%') "
                        "ORDER BY occurred_at DESC, created_at DESC LIMIT 1",
                        (f"%{desc}%", desc),
                    ).fetchone()
                if row is not None:
                    learned = {
                        "kind": row["kind"],
                        "category": row["category"],
                        "matched_description": row["description"],
                        "last_seen": row["occurred_at"],
                    }
        return assess_parsed_entry(p, learned_from_history=learned)

    def list_transactions(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        kind: Kind | None = None,
        category: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        where = ["deleted_at IS NULL"]
        args: list[Any] = []
        if start_date:
            where.append("substr(occurred_at,1,10) >= ?")
            args.append(normalize_date(start_date))
        if end_date:
            where.append("substr(occurred_at,1,10) <= ?")
            args.append(normalize_date(end_date))
        if kind:
            where.append("kind=?")
            args.append(kind)
        if category:
            where.append("category=?")
            args.append(category)
        if query:
            where.append("(description LIKE ? OR category LIKE ? OR note LIKE ? OR raw_input LIKE ?)")
            q = f"%{query}%"
            args.extend([q, q, q, q])
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM transactions WHERE {' AND '.join(where)} ORDER BY occurred_at DESC, created_at DESC LIMIT ?",
                args,
            ).fetchall()
            return [self._public(r) for r in rows]

    def _period_dates(self, period: str, start_date: str | None, end_date: str | None) -> tuple[str, str]:
        today = now_local().date()
        if period == "today":
            return today.isoformat(), today.isoformat()
        if period == "yesterday":
            d = today - timedelta(days=1)
            return d.isoformat(), d.isoformat()
        if period == "this_week":
            start = today - timedelta(days=today.weekday())
            return start.isoformat(), today.isoformat()
        if period == "this_month":
            return today.replace(day=1).isoformat(), today.isoformat()
        if period == "last_month":
            first = today.replace(day=1)
            last = first - timedelta(days=1)
            return last.replace(day=1).isoformat(), last.isoformat()
        if period == "custom":
            if not start_date or not end_date:
                raise ValueError("period=custom ต้องส่ง start_date และ end_date")
            return normalize_date(start_date), normalize_date(end_date)
        raise ValueError("period ต้องเป็น today, yesterday, this_week, this_month, last_month หรือ custom")

    def summary(self, period: str = "this_month", start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        start, end = self._period_dates(period, start_date, end_date)
        with self.connect() as conn:
            totals = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE 0 END),0) income,
                  COALESCE(SUM(CASE WHEN kind='expense' THEN amount_minor ELSE 0 END),0) expense,
                  COUNT(*) count
                FROM transactions
                WHERE deleted_at IS NULL AND substr(occurred_at,1,10) BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchone()
            categories = conn.execute(
                """
                SELECT category, SUM(amount_minor) amount_minor, COUNT(*) count
                FROM transactions
                WHERE deleted_at IS NULL AND kind='expense' AND substr(occurred_at,1,10) BETWEEN ? AND ?
                GROUP BY category ORDER BY amount_minor DESC LIMIT 10
                """,
                (start, end),
            ).fetchall()
            incomes = conn.execute(
                """
                SELECT category, SUM(amount_minor) amount_minor, COUNT(*) count
                FROM transactions
                WHERE deleted_at IS NULL AND kind='income' AND substr(occurred_at,1,10) BETWEEN ? AND ?
                GROUP BY category ORDER BY amount_minor DESC LIMIT 10
                """,
                (start, end),
            ).fetchall()
        income = int(totals["income"])
        expense = int(totals["expense"])
        return {
            "period": period,
            "start_date": start,
            "end_date": end,
            "currency": DEFAULT_CURRENCY,
            "income": minor_to_money(income),
            "expense": minor_to_money(expense),
            "net": minor_to_money(income - expense),
            "transaction_count": int(totals["count"]),
            "top_expense_categories": [
                {"category": r["category"], "amount": minor_to_money(int(r["amount_minor"])), "count": int(r["count"])}
                for r in categories
            ],
            "top_income_categories": [
                {"category": r["category"], "amount": minor_to_money(int(r["amount_minor"])), "count": int(r["count"])}
                for r in incomes
            ],
        }

    def financial_overview(self, period: str = "this_month", start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        current = self.summary(period, start_date, end_date)
        lifetime = self.summary("custom", "1970-01-01", now_local().date().isoformat())
        income = money_to_minor(current["income"]) if current["income"] != "0.00" else 0
        expense = money_to_minor(current["expense"]) if current["expense"] != "0.00" else 0
        net = income - expense
        start = date.fromisoformat(current["start_date"]); end = date.fromisoformat(current["end_date"])
        days = max(1, (end - start).days + 1)
        savings_rate = (net / income * 100) if income > 0 else None
        return {
            "period": period, "start_date": current["start_date"], "end_date": current["end_date"],
            "currency": DEFAULT_CURRENCY, "income": current["income"], "expense": current["expense"], "net_saved": current["net"],
            "lifetime_net_saved": lifetime["net"], "average_daily_expense": minor_to_money(expense // days),
            "savings_rate_percent": round(savings_rate, 2) if savings_rate is not None else None,
            "top_expense_categories": current["top_expense_categories"], "transaction_count": current["transaction_count"]
        }

    def set_budget(self, category: str, amount: str | int | float | Decimal, warning_percent: int = 80) -> dict[str, Any]:
        category = category.strip().lower()
        if not category: raise ValueError("category ว่างไม่ได้")
        amount_minor = money_to_minor(amount); warning_percent = max(1, min(int(warning_percent), 100)); now = iso_now()
        with self.connect() as conn:
            conn.execute("INSERT INTO budgets(category,amount_minor,period,warning_percent,created_at,updated_at) VALUES(?,?,'monthly',?,?,?) ON CONFLICT(category) DO UPDATE SET amount_minor=excluded.amount_minor,warning_percent=excluded.warning_percent,updated_at=excluded.updated_at", (category,amount_minor,warning_percent,now,now))
        return self.budget_status(category)

    def list_budgets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows=conn.execute("SELECT category,amount_minor,warning_percent,created_at,updated_at FROM budgets ORDER BY category").fetchall()
        return [{"category":r["category"],"amount":minor_to_money(r["amount_minor"]),"warning_percent":r["warning_percent"],"created_at":r["created_at"],"updated_at":r["updated_at"]} for r in rows]

    def budget_status(self, category: str | None = None) -> dict[str, Any]:
        start=now_local().date().replace(day=1).isoformat(); end=now_local().date().isoformat()
        with self.connect() as conn:
            if category:
                rows=conn.execute("SELECT * FROM budgets WHERE category=?",(category.strip().lower(),)).fetchall()
            else:
                rows=conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()
            out=[]
            for r in rows:
                spent=conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE deleted_at IS NULL AND kind='expense' AND category=? AND substr(occurred_at,1,10) BETWEEN ? AND ?",(r["category"],start,end)).fetchone()[0]
                limit=int(r["amount_minor"]); pct=(spent/limit*100) if limit else 0
                status="over" if spent>limit else ("warning" if pct>=int(r["warning_percent"]) else "ok")
                out.append({"category":r["category"],"budget":minor_to_money(limit),"spent":minor_to_money(int(spent)),"remaining":minor_to_money(max(0,limit-int(spent))),"used_percent":round(pct,2),"warning_percent":int(r["warning_percent"]),"status":status})
        return {"month":start[:7],"currency":DEFAULT_CURRENCY,"budgets":out}

    def set_savings_goal(self, name: str, target: str | int | float | Decimal, target_date: str | None = None, saved: str | int | float | Decimal | None = None) -> dict[str, Any]:
        name=name.strip();
        if not name: raise ValueError("ชื่อเป้าหมายว่างไม่ได้")
        target_minor=money_to_minor(target); saved_minor=money_to_minor(saved) if saved not in (None,"",0,"0","0.00") else 0
        td=normalize_date(target_date) if target_date else None; now=iso_now(); gid=str(uuid.uuid4())
        with self.connect() as conn:
            existing=conn.execute("SELECT id FROM savings_goals WHERE name=?",(name,)).fetchone()
            if existing:
                gid=existing["id"]; conn.execute("UPDATE savings_goals SET target_minor=?,saved_minor=?,target_date=?,updated_at=? WHERE id=?",(target_minor,saved_minor,td,now,gid))
            else:
                conn.execute("INSERT INTO savings_goals(id,name,target_minor,saved_minor,target_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(gid,name,target_minor,saved_minor,td,now,now))
        return self.get_savings_goals(name)[0]

    def get_savings_goals(self, name: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows=conn.execute("SELECT * FROM savings_goals" + (" WHERE name=?" if name else "") + " ORDER BY created_at", ((name,) if name else ())).fetchall()
        out=[]
        for r in rows:
            target=int(r["target_minor"]); saved=int(r["saved_minor"]); remaining=max(0,target-saved)
            out.append({"id":r["id"],"name":r["name"],"target":minor_to_money(target),"saved":minor_to_money(saved),"remaining":minor_to_money(remaining),"progress_percent":round(saved/target*100,2),"target_date":r["target_date"]})
        return out

    def add_savings(self, name: str, amount: str | int | float | Decimal) -> dict[str, Any]:
        delta=money_to_minor(amount)
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM savings_goals WHERE name=?",(name,)).fetchone()
            if not row: raise ValueError("ไม่พบเป้าหมายเงินออม")
            conn.execute("UPDATE savings_goals SET saved_minor=saved_minor+?,updated_at=? WHERE id=?",(delta,iso_now(),row["id"]))
        return self.get_savings_goals(name)[0]

    def financial_alerts(self) -> list[dict[str, Any]]:
        alerts=[]; s=self.summary("this_month"); income=Decimal(s["income"].replace(",","")); expense=Decimal(s["expense"].replace(",",""))
        if expense>income and expense>0: alerts.append({"level":"danger","type":"negative_cashflow","message":f"เดือนนี้รายจ่าย {s['expense']} มากกว่ารายรับ {s['income']} {DEFAULT_CURRENCY}"})
        for b in self.budget_status()["budgets"]:
            if b["status"]=="over": alerts.append({"level":"danger","type":"budget_over","category":b["category"],"message":f"หมวด {b['category']} เกินงบแล้ว ใช้ {b['spent']} / {b['budget']} {DEFAULT_CURRENCY}"})
            elif b["status"]=="warning": alerts.append({"level":"warning","type":"budget_warning","category":b["category"],"message":f"หมวด {b['category']} ใช้งบไป {b['used_percent']}% ({b['spent']} / {b['budget']} {DEFAULT_CURRENCY})"})
        return alerts

    def compare_months(self, months: int = 3) -> list[dict[str, Any]]:
        months = max(1, min(int(months), 24))
        today = now_local().date()
        out: list[dict[str, Any]] = []
        year, month = today.year, today.month
        for offset in range(months):
            m = month - offset
            y = year
            while m <= 0:
                m += 12
                y -= 1
            start = date(y, m, 1)
            if m == 12:
                next_month = date(y + 1, 1, 1)
            else:
                next_month = date(y, m + 1, 1)
            end = min(today, next_month - timedelta(days=1)) if offset == 0 else next_month - timedelta(days=1)
            s = self.summary("custom", start.isoformat(), end.isoformat())
            s["month"] = f"{y:04d}-{m:02d}"
            out.append(s)
        return out

    def backup(self, destination: Path | str) -> str:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return str(destination)


def _thai_date_label(value: str) -> str:
    d = date.fromisoformat(value[:10])
    months = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")
    return f"{d.day} {months[d.month - 1]} {d.year}"


def _category_label(value: str) -> str:
    labels = {
        "food": "อาหาร/เครื่องดื่ม",
        "อาหาร/เครื่องดื่ม": "อาหาร/เครื่องดื่ม",
        "transport": "เดินทาง",
        "server_hosting": "Server/Hosting",
        "ai": "AI/Software",
        "domain": "โดเมน",
        "software": "ซอฟต์แวร์",
        "utilities": "สาธารณูปโภค",
        "shopping": "ซื้อของ",
        "education": "การศึกษา",
        "entertainment": "บันเทิง",
        "work_income": "รายได้จากงาน",
        "sales_income": "รายได้จากการขาย",
        "income_other": "รายรับอื่นๆ",
        "other": "อื่นๆ",
    }
    return labels.get(value, value.replace("_", " "))


def format_summary(summary: dict[str, Any], balance: str | None = None) -> str:
    if summary["start_date"] == summary["end_date"]:
        heading = "วันนี้" if summary.get("period") == "today" else _thai_date_label(summary["start_date"])
    else:
        heading = f"{_thai_date_label(summary['start_date'])} – {_thai_date_label(summary['end_date'])}"
    lines = [
        f"📊 {heading}",
        f"รับ    +{summary['income']} บาท",
        f"จ่าย   -{summary['expense']} บาท",
        f"สุทธิ  {('+' if not str(summary['net']).startswith('-') else '')}{summary['net']} บาท",
    ]
    if balance is not None:
        lines.append(f"รวม    {balance} บาท")
    return "\n".join(lines)


def format_transactions(rows: list[dict[str, Any]], *, balance: str | None = None) -> str:
    if not rows:
        return "📒 ยังไม่มีรายการ" + (f"\nรวม    {balance} บาท" if balance is not None else "")
    lines = ["📒 รายการบัญชี"]
    current_day: str | None = None
    income = Decimal("0")
    expense = Decimal("0")
    for tx in rows:
        day = str(tx["occurred_at"])[:10]
        if day != current_day:
            current_day = day
            lines.append(f"\n{_thai_date_label(day)}")
        amount = Decimal(str(tx["amount"]).replace(",", ""))
        sign = "+" if tx["kind"] == "income" else "-"
        if tx["kind"] == "income":
            income += amount
        else:
            expense += amount
        category = _category_label(str(tx.get("category") or "other"))
        lines.append(f"{sign}{amount:,.2f}  {tx['description']} · {category}")
    net = income - expense
    lines += [
        "\n────────",
        f"รับ    +{income:,.2f} บาท",
        f"จ่าย   -{expense:,.2f} บาท",
        f"สุทธิ  {net:+,.2f} บาท",
    ]
    if balance is not None:
        lines.append(f"รวม    {balance} บาท")
    return "\n".join(lines)
