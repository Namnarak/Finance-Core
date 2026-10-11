from __future__ import annotations

import calendar
import math
import statistics
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from .config import DEFAULT_CURRENCY, PRIMARY_ACCOUNT
from .core import FinanceDB, iso_now, minor_to_money, money_to_minor, normalize_date, now_local

AccountType = Literal["cash", "bank", "wallet", "savings", "investment", "other"]
DebtKind = Literal["payable", "receivable"]
Cadence = Literal["daily", "weekly", "monthly", "yearly"]

ADVANCED_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL DEFAULT 'bank',
    opening_balance_minor INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'THB',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_balance_adjustments (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    old_opening_balance_minor INTEGER NOT NULL,
    new_opening_balance_minor INTEGER NOT NULL,
    balance_before_minor INTEGER NOT NULL,
    target_balance_minor INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY,
    from_account_id TEXT NOT NULL REFERENCES accounts(id),
    to_account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    occurred_at TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    category TEXT NOT NULL DEFAULT 'other',
    description TEXT NOT NULL,
    cadence TEXT NOT NULL CHECK(cadence IN ('daily','weekly','monthly','yearly')),
    next_due_date TEXT NOT NULL,
    account_id TEXT REFERENCES accounts(id),
    payment_method TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_posted_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    cadence TEXT NOT NULL CHECK(cadence IN ('weekly','monthly','yearly')),
    next_due_date TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'software',
    account_id TEXT REFERENCES accounts(id),
    note TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('payable','receivable')),
    original_minor INTEGER NOT NULL CHECK(original_minor > 0),
    remaining_minor INTEGER NOT NULL CHECK(remaining_minor >= 0),
    due_date TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debt_payments (
    id TEXT PRIMARY KEY,
    debt_id TEXT NOT NULL REFERENCES debts(id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    occurred_at TEXT NOT NULL,
    transaction_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfers_date ON transfers(occurred_at);
CREATE INDEX IF NOT EXISTS idx_recurring_due ON recurring_items(enabled,next_due_date);
CREATE INDEX IF NOT EXISTS idx_subscriptions_due ON subscriptions(enabled,next_due_date);
CREATE INDEX IF NOT EXISTS idx_debts_due ON debts(due_date);
"""


def _advance_due(d: date, cadence: str) -> date:
    if cadence == "daily":
        return d + timedelta(days=1)
    if cadence == "weekly":
        return d + timedelta(days=7)
    if cadence == "monthly":
        y, m = d.year, d.month + 1
        if m == 13:
            y, m = y + 1, 1
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
    if cadence == "yearly":
        y = d.year + 1
        return date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))
    raise ValueError("cadence ต้องเป็น daily/weekly/monthly/yearly")


class AdvancedFinanceDB(FinanceDB):
    def init(self) -> None:
        super().init()
        with self.connect() as conn:
            conn.executescript(ADVANCED_SCHEMA)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
            if "account_id" not in columns:
                conn.execute("ALTER TABLE transactions ADD COLUMN account_id TEXT REFERENCES accounts(id)")

    def _account(self, conn, account: str | None):
        if not account:
            return None
        row = conn.execute("SELECT * FROM accounts WHERE id=? OR lower(name)=lower(?)", (account, account)).fetchone()
        if not row:
            raise ValueError(f"ไม่พบบัญชี: {account}")
        return row

    def _primary_account_row(self, conn):
        setting = conn.execute("SELECT value FROM finance_settings WHERE key='primary_account'").fetchone()
        preferred = setting["value"] if setting else PRIMARY_ACCOUNT
        if preferred:
            row = conn.execute(
                "SELECT * FROM accounts WHERE is_active=1 AND (id=? OR lower(name)=lower(?))",
                (preferred, preferred),
            ).fetchone()
            if row:
                return row
        return None

    def _transaction_account(self, conn, account: str | None):
        """Resolve explicit account, then configured primary account, then sole active account."""
        if account:
            return self._account(conn, account)
        primary = self._primary_account_row(conn)
        if primary:
            return primary
        rows = conn.execute("SELECT * FROM accounts WHERE is_active=1 ORDER BY created_at").fetchall()
        return rows[0] if len(rows) == 1 else None

    def set_primary_account(self, account: str) -> dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            row = self._account(conn, account)
            if not row["is_active"]:
                raise ValueError("บัญชีหลักต้องเป็นบัญชีที่เปิดใช้งาน")
            conn.execute(
                "INSERT INTO finance_settings(key,value,updated_at) VALUES('primary_account',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (row["id"], now),
            )
        return self.get_primary_account()

    def get_primary_account(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = self._primary_account_row(conn)
            aid = row["id"] if row else None
        return self.get_account(aid) if aid else None

    def set_account_active(self, account: str, active: bool) -> dict[str, Any]:
        with self.connect() as conn:
            row = self._account(conn, account)
            if not active:
                primary = self._primary_account_row(conn)
                if primary and primary["id"] == row["id"]:
                    raise ValueError("ปิดบัญชีหลักไม่ได้ กรุณาเปลี่ยนบัญชีหลักก่อน")
            conn.execute("UPDATE accounts SET is_active=?,updated_at=? WHERE id=?", (1 if active else 0, iso_now(), row["id"]))
        return self.get_account(row["id"])

    def reconcile_account_balance(self, account: str, balance: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if not reason:
            raise ValueError("ต้องระบุเหตุผลในการปรับยอด")
        target = 0 if str(balance).strip() in {"0", "0.0", "0.00"} else money_to_minor(balance)
        with self.connect() as conn:
            row = self._account(conn, account)
            aid = row["id"]
        before = self.get_account(aid)
        before_minor = money_to_minor(before["balance"].replace(",", ""))
        with self.connect() as conn:
            row = self._account(conn, aid)
            old_opening = int(row["opening_balance_minor"])
            new_opening = old_opening + (target - before_minor)
            now = iso_now()
            conn.execute("UPDATE accounts SET opening_balance_minor=?,updated_at=? WHERE id=?", (new_opening, now, aid))
            conn.execute(
                "INSERT INTO account_balance_adjustments(id,account_id,old_opening_balance_minor,new_opening_balance_minor,balance_before_minor,target_balance_minor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), aid, old_opening, new_opening, before_minor, target, reason, now),
            )
        after = self.get_account(aid)
        return {"account": after, "balance_before": before["balance"], "target_balance": minor_to_money(target), "reason": reason}

    def add_transaction(self, *args: Any, account: str | None = None, **kwargs: Any) -> dict[str, Any]:
        result = super().add_transaction(*args, **kwargs)
        if not result.get("duplicate_prevented"):
            with self.connect() as conn:
                a = self._transaction_account(conn, account)
                if a:
                    conn.execute("UPDATE transactions SET account_id=?,updated_at=? WHERE id=?", (a["id"], iso_now(), result["id"]))
            result = self.get_transaction(result["id"]) or result
        return result

    def create_account(self, name: str, account_type: AccountType = "bank", opening_balance: str = "0") -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("ชื่อบัญชีว่างไม่ได้")
        allowed = {"cash", "bank", "wallet", "savings", "investment", "other"}
        if account_type not in allowed:
            raise ValueError("account_type ไม่ถูกต้อง")
        opening = 0 if str(opening_balance).strip() in {"", "0", "0.0", "0.00"} else money_to_minor(opening_balance)
        now = iso_now(); aid = str(uuid.uuid4())
        with self.connect() as conn:
            existing = conn.execute("SELECT id FROM accounts WHERE lower(name)=lower(?)", (name,)).fetchone()
            if existing:
                aid = existing["id"]
                conn.execute("UPDATE accounts SET account_type=?,opening_balance_minor=?,is_active=1,updated_at=? WHERE id=?", (account_type, opening, now, aid))
            else:
                conn.execute("INSERT INTO accounts(id,name,account_type,opening_balance_minor,currency,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (aid,name,account_type,opening,DEFAULT_CURRENCY,now,now))
        return self.get_account(aid)

    def get_account(self, account: str) -> dict[str, Any]:
        with self.connect() as conn:
            a = self._account(conn, account)
            tx = conn.execute("SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE -amount_minor END),0) AS delta FROM transactions WHERE deleted_at IS NULL AND account_id=?", (a["id"],)).fetchone()["delta"]
            incoming = conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transfers WHERE to_account_id=?", (a["id"],)).fetchone()[0]
            outgoing = conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transfers WHERE from_account_id=?", (a["id"],)).fetchone()[0]
            balance = int(a["opening_balance_minor"]) + int(tx) + int(incoming) - int(outgoing)
            primary = self._primary_account_row(conn)
            return {"id":a["id"],"name":a["name"],"type":a["account_type"],"balance":minor_to_money(balance),"opening_balance":minor_to_money(int(a["opening_balance_minor"])),"currency":a["currency"],"active":bool(a["is_active"]),"primary":bool(primary and primary["id"] == a["id"])}

    def list_accounts(self, active_only: bool = True) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM accounts" + (" WHERE is_active=1" if active_only else "") + " ORDER BY name").fetchall()
        return [self.get_account(r["id"]) for r in rows]

    def transfer(self, from_account: str, to_account: str, amount: str, occurred_at: str | None = None, note: str | None = None) -> dict[str, Any]:
        amt = money_to_minor(amount); when = (occurred_at or iso_now())
        if occurred_at:
            from .core import normalize_datetime
            when = normalize_datetime(occurred_at)
        with self.connect() as conn:
            src = self._account(conn, from_account); dst = self._account(conn, to_account)
            if src["id"] == dst["id"]:
                raise ValueError("ต้นทางและปลายทางต้องเป็นคนละบัญชี")
            tid = str(uuid.uuid4())
            conn.execute("INSERT INTO transfers(id,from_account_id,to_account_id,amount_minor,occurred_at,note,created_at) VALUES(?,?,?,?,?,?,?)", (tid,src["id"],dst["id"],amt,when,note,iso_now()))
        return {"id":tid,"from":self.get_account(src["id"]),"to":self.get_account(dst["id"]),"amount":minor_to_money(amt),"occurred_at":when}

    def set_recurring(self, name: str, kind: str, amount: str, description: str, category: str, cadence: Cadence, next_due_date: str, account: str | None = None, payment_method: str | None = None) -> dict[str, Any]:
        if kind not in {"income","expense"}: raise ValueError("kind ต้องเป็น income หรือ expense")
        if cadence not in {"daily","weekly","monthly","yearly"}: raise ValueError("cadence ไม่ถูกต้อง")
        due = normalize_date(next_due_date); amt = money_to_minor(amount); now=iso_now(); rid=str(uuid.uuid4()); account_id=None
        with self.connect() as conn:
            if account: account_id=self._account(conn,account)["id"]
            ex=conn.execute("SELECT id FROM recurring_items WHERE lower(name)=lower(?)",(name,)).fetchone()
            if ex:
                rid=ex["id"]; conn.execute("UPDATE recurring_items SET kind=?,amount_minor=?,category=?,description=?,cadence=?,next_due_date=?,account_id=?,payment_method=?,enabled=1,updated_at=? WHERE id=?",(kind,amt,category,description,cadence,due,account_id,payment_method,now,rid))
            else:
                conn.execute("INSERT INTO recurring_items(id,name,kind,amount_minor,category,description,cadence,next_due_date,account_id,payment_method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(rid,name,kind,amt,category,description,cadence,due,account_id,payment_method,now,now))
        return self.get_recurring(name)[0]

    def get_recurring(self, name: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
        q="SELECT r.*,a.name account_name FROM recurring_items r LEFT JOIN accounts a ON a.id=r.account_id WHERE 1=1"; params=[]
        if name: q+=" AND lower(r.name)=lower(?)"; params.append(name)
        if enabled_only: q+=" AND r.enabled=1"
        q+=" ORDER BY r.next_due_date,r.name"
        with self.connect() as conn: rows=conn.execute(q,params).fetchall()
        return [{"id":r["id"],"name":r["name"],"kind":r["kind"],"amount":minor_to_money(int(r["amount_minor"])),"category":r["category"],"description":r["description"],"cadence":r["cadence"],"next_due_date":r["next_due_date"],"account":r["account_name"],"payment_method":r["payment_method"],"enabled":bool(r["enabled"]),"last_posted_date":r["last_posted_date"]} for r in rows]

    def process_due_recurring(self, through_date: str | None = None) -> dict[str, Any]:
        through=normalize_date(through_date) if through_date else now_local().date().isoformat(); posted=[]
        with self.connect() as conn:
            rows=conn.execute("SELECT r.*,a.name account_name FROM recurring_items r LEFT JOIN accounts a ON a.id=r.account_id WHERE r.enabled=1 AND r.next_due_date<=? ORDER BY r.next_due_date",(through,)).fetchall()
        for r in rows:
            due=date.fromisoformat(r["next_due_date"])
            while due.isoformat() <= through:
                key=f"recurring:{r['id']}:{due.isoformat()}"
                tx=self.add_transaction(kind=r["kind"],amount=minor_to_money(int(r["amount_minor"])),description=r["description"],category=r["category"],occurred_at=due.isoformat(),payment_method=r["payment_method"],note=f"Recurring: {r['name']}",idempotency_key=key,allow_duplicate=True,account=r["account_name"])
                posted.append({"recurring":r["name"],"due_date":due.isoformat(),"transaction_id":tx["id"]})
                due=_advance_due(due,r["cadence"])
            with self.connect() as conn:
                conn.execute("UPDATE recurring_items SET next_due_date=?,last_posted_date=?,updated_at=? WHERE id=?",(due.isoformat(),posted[-1]["due_date"] if posted else r["last_posted_date"],iso_now(),r["id"]))
        return {"through_date":through,"posted_count":len(posted),"posted":posted}

    def set_subscription(self, name: str, amount: str, cadence: Literal["weekly","monthly","yearly"], next_due_date: str, category: str="software", account: str | None=None, note: str | None=None) -> dict[str, Any]:
        if cadence not in {"weekly","monthly","yearly"}: raise ValueError("cadence ไม่ถูกต้อง")
        due=normalize_date(next_due_date); amt=money_to_minor(amount); now=iso_now(); sid=str(uuid.uuid4()); account_id=None
        with self.connect() as conn:
            if account: account_id=self._account(conn,account)["id"]
            ex=conn.execute("SELECT id FROM subscriptions WHERE lower(name)=lower(?)",(name,)).fetchone()
            if ex:
                sid=ex["id"]; conn.execute("UPDATE subscriptions SET amount_minor=?,cadence=?,next_due_date=?,category=?,account_id=?,note=?,enabled=1,updated_at=? WHERE id=?",(amt,cadence,due,category,account_id,note,now,sid))
            else:
                conn.execute("INSERT INTO subscriptions(id,name,amount_minor,cadence,next_due_date,category,account_id,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(sid,name,amt,cadence,due,category,account_id,note,now,now))
        return self.list_subscriptions(name)[0]

    def list_subscriptions(self, name: str | None=None, enabled_only: bool=True) -> list[dict[str, Any]]:
        q="SELECT s.*,a.name account_name FROM subscriptions s LEFT JOIN accounts a ON a.id=s.account_id WHERE 1=1"; params=[]
        if name: q+=" AND lower(s.name)=lower(?)"; params.append(name)
        if enabled_only: q+=" AND s.enabled=1"
        q+=" ORDER BY s.next_due_date,s.name"
        with self.connect() as conn: rows=conn.execute(q,params).fetchall()
        today=now_local().date()
        return [{"id":r["id"],"name":r["name"],"amount":minor_to_money(int(r["amount_minor"])),"cadence":r["cadence"],"next_due_date":r["next_due_date"],"days_until_due":(date.fromisoformat(r["next_due_date"])-today).days,"category":r["category"],"account":r["account_name"],"note":r["note"],"enabled":bool(r["enabled"])} for r in rows]

    def set_debt(self, name: str, kind: DebtKind, amount: str, due_date: str | None=None, note: str | None=None) -> dict[str, Any]:
        if kind not in {"payable","receivable"}: raise ValueError("kind ต้องเป็น payable หรือ receivable")
        amt=money_to_minor(amount); due=normalize_date(due_date) if due_date else None; now=iso_now(); did=str(uuid.uuid4())
        with self.connect() as conn:
            ex=conn.execute("SELECT id FROM debts WHERE lower(name)=lower(?)",(name,)).fetchone()
            if ex:
                did=ex["id"]; conn.execute("UPDATE debts SET kind=?,original_minor=?,remaining_minor=?,due_date=?,note=?,updated_at=? WHERE id=?",(kind,amt,amt,due,note,now,did))
            else:
                conn.execute("INSERT INTO debts(id,name,kind,original_minor,remaining_minor,due_date,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(did,name,kind,amt,amt,due,note,now,now))
        return self.list_debts(name)[0]

    def list_debts(self, name: str | None=None, open_only: bool=True) -> list[dict[str, Any]]:
        q="SELECT * FROM debts WHERE 1=1"; params=[]
        if name: q+=" AND lower(name)=lower(?)"; params.append(name)
        if open_only: q+=" AND remaining_minor>0"
        q+=" ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,due_date,name"
        with self.connect() as conn: rows=conn.execute(q,params).fetchall()
        return [{"id":r["id"],"name":r["name"],"kind":r["kind"],"original":minor_to_money(int(r["original_minor"])),"remaining":minor_to_money(int(r["remaining_minor"])),"paid":minor_to_money(int(r["original_minor"])-int(r["remaining_minor"])),"progress_percent":round((int(r["original_minor"])-int(r["remaining_minor"]))/int(r["original_minor"])*100,2),"due_date":r["due_date"],"note":r["note"]} for r in rows]

    def pay_debt(self, name: str, amount: str, account: str | None=None, record_transaction: bool=True) -> dict[str, Any]:
        amt=money_to_minor(amount)
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM debts WHERE lower(name)=lower(?)",(name,)).fetchone()
            if not row: raise ValueError("ไม่พบหนี้/ลูกหนี้")
            if amt>int(row["remaining_minor"]): raise ValueError("จำนวนเงินมากกว่ายอดคงเหลือ")
            remaining=int(row["remaining_minor"])-amt
            conn.execute("UPDATE debts SET remaining_minor=?,updated_at=? WHERE id=?",(remaining,iso_now(),row["id"]))
        txid=None
        if record_transaction:
            kind="expense" if row["kind"]=="payable" else "income"
            tx=self.add_transaction(kind=kind,amount=minor_to_money(amt),description=f"ชำระ {name}",category="debt",occurred_at="วันนี้",note=f"Debt payment: {row['id']}",account=account)
            txid=tx["id"]
        with self.connect() as conn:
            conn.execute("INSERT INTO debt_payments(id,debt_id,amount_minor,occurred_at,transaction_id,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),row["id"],amt,iso_now(),txid,iso_now()))
        return self.list_debts(name,open_only=False)[0]

    def month_forecast(self, target_savings: str="0") -> dict[str, Any]:
        today=now_local().date(); month_start=today.replace(day=1); month_end=date(today.year,today.month,calendar.monthrange(today.year,today.month)[1])
        s=self.summary("custom",month_start.isoformat(),today.isoformat())
        income=Decimal(s["income"].replace(",","")); expense=Decimal(s["expense"].replace(",","")); elapsed=max(1,today.day); total_days=month_end.day
        target=Decimal(str(target_savings).replace(",",""))
        daily_expense=expense/elapsed
        scheduled_expense=Decimal("0"); scheduled_income=Decimal("0")
        with self.connect() as conn:
            rec=conn.execute("SELECT kind,amount_minor,next_due_date,cadence FROM recurring_items WHERE enabled=1 AND next_due_date BETWEEN ? AND ?",(today.isoformat(),month_end.isoformat())).fetchall()
            subs=conn.execute("SELECT amount_minor FROM subscriptions WHERE enabled=1 AND next_due_date BETWEEN ? AND ?",(today.isoformat(),month_end.isoformat())).fetchall()
        for r in rec:
            val=Decimal(int(r["amount_minor"]))/100
            if r["kind"]=="expense": scheduled_expense+=val
            else: scheduled_income+=val
        scheduled_expense += sum((Decimal(int(r["amount_minor"]))/100 for r in subs),Decimal("0"))
        projected_expense=expense + daily_expense*Decimal(max(0,total_days-elapsed)) + scheduled_expense
        projected_income=income + scheduled_income
        projected_net=projected_income-projected_expense
        remaining_days=max(1,(month_end-today).days+1)
        safe_daily=(income+scheduled_income-expense-scheduled_expense-target)/Decimal(remaining_days)
        return {"month":today.strftime("%Y-%m"),"currency":DEFAULT_CURRENCY,"income_to_date":f"{income:,.2f}","expense_to_date":f"{expense:,.2f}","average_daily_expense":f"{daily_expense:,.2f}","scheduled_remaining_expense":f"{scheduled_expense:,.2f}","scheduled_remaining_income":f"{scheduled_income:,.2f}","projected_month_end_income":f"{projected_income:,.2f}","projected_month_end_expense":f"{projected_expense:,.2f}","projected_month_end_net":f"{projected_net:,.2f}","target_savings":f"{target:,.2f}","safe_daily_allowance_from_known_cashflow":f"{safe_daily:,.2f}","remaining_days_including_today":remaining_days,"method":"expense run-rate + known scheduled items; unscheduled future income is not guessed"}

    def anomalies(self, days: int=90, min_samples: int=3) -> list[dict[str, Any]]:
        days=max(7,min(int(days),365)); start=(now_local().date()-timedelta(days=days)).isoformat()
        with self.connect() as conn:
            rows=conn.execute("SELECT id,category,description,amount_minor,occurred_at FROM transactions WHERE deleted_at IS NULL AND kind='expense' AND substr(occurred_at,1,10)>=? ORDER BY occurred_at",(start,)).fetchall()
        bycat: dict[str,list[int]]={}
        for r in rows: bycat.setdefault(r["category"],[]).append(int(r["amount_minor"]))
        out=[]
        for r in rows:
            vals=bycat[r["category"]]
            if len(vals)<min_samples: continue
            baseline=[v for v in vals if v!=int(r["amount_minor"])] or vals
            mean=statistics.mean(baseline)
            sd=statistics.pstdev(baseline) if len(baseline)>1 else 0
            threshold=max(mean*2,mean+2*sd)
            if int(r["amount_minor"])>threshold and int(r["amount_minor"])>0:
                out.append({"transaction_id":r["id"],"category":r["category"],"description":r["description"],"amount":minor_to_money(int(r["amount_minor"])),"category_average":minor_to_money(int(mean)),"times_average":round(int(r["amount_minor"])/mean,2) if mean else None,"occurred_at":r["occurred_at"],"reason":"expense materially above historical category baseline"})
        return sorted(out,key=lambda x: float(x["times_average"] or 0),reverse=True)[:20]

    def net_worth(self) -> dict[str, Any]:
        accounts=self.list_accounts(); account_total=sum((Decimal(a["balance"].replace(",","")) for a in accounts),Decimal("0"))
        debts=self.list_debts(open_only=True); receivable=sum((Decimal(d["remaining"].replace(",","")) for d in debts if d["kind"]=="receivable"),Decimal("0")); payable=sum((Decimal(d["remaining"].replace(",","")) for d in debts if d["kind"]=="payable"),Decimal("0"))
        return {"currency":DEFAULT_CURRENCY,"account_balance_total":f"{account_total:,.2f}","receivables":f"{receivable:,.2f}","payables":f"{payable:,.2f}","net_worth":f"{account_total+receivable-payable:,.2f}","accounts":accounts}

    def advanced_alerts(self, upcoming_days: int=7) -> list[dict[str, Any]]:
        alerts=list(self.financial_alerts()); today=now_local().date(); cutoff=today+timedelta(days=max(1,min(int(upcoming_days),60)))
        for s in self.list_subscriptions(enabled_only=True):
            due=date.fromisoformat(s["next_due_date"])
            if today<=due<=cutoff: alerts.append({"level":"info" if due>today else "warning","type":"subscription_due","name":s["name"],"amount":s["amount"],"due_date":s["next_due_date"],"message":f"{s['name']} จะตัด {s['amount']} {DEFAULT_CURRENCY} วันที่ {s['next_due_date']}"})
        for d in self.list_debts(open_only=True):
            if d["due_date"]:
                due=date.fromisoformat(d["due_date"])
                if due<today: alerts.append({"level":"danger","type":"debt_overdue","name":d["name"],"amount":d["remaining"],"due_date":d["due_date"],"message":f"{d['name']} เลยกำหนดแล้ว คงเหลือ {d['remaining']} {DEFAULT_CURRENCY}"})
                elif due<=cutoff: alerts.append({"level":"warning","type":"debt_due","name":d["name"],"amount":d["remaining"],"due_date":d["due_date"],"message":f"{d['name']} ครบกำหนด {d['due_date']} คงเหลือ {d['remaining']} {DEFAULT_CURRENCY}"})
        return alerts
