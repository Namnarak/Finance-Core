from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .advanced import AdvancedFinanceDB
from .core import format_summary, format_transactions, parse_simple_entry

mcp = MCPServer(
    "NamKrub Finance",
    version="2.2.0",
    instructions=(
        "ระบบรายรับรายจ่ายส่วนตัว ใช้ข้อมูลจากฐานข้อมูลจริงเท่านั้น ห้ามเดายอดเงินจากบทสนทนา "
        "หลักสำคัญ: แน่ใจค่อยบันทึก ไม่แน่ใจต้องถามผู้ใช้ก่อน โดยเฉพาะข้อความสั้น รายการไม่ชัด หมวดไม่ชัด หรือไม่รู้ว่าเงินเข้า/ออก "
        "ใช้ analyze_entry เพื่อตรวจข้อความกำกวมโดยไม่เขียนฐานข้อมูล และ record_entry จะไม่บันทึกรายการที่ต้องถามเพิ่ม เว้นแต่ผู้ใช้ยืนยันให้ลงแบบกำกวม "
        "เมื่อผู้ใช้ตอบคำถามเพิ่ม ให้รวมข้อมูลจากข้อความก่อนหน้าแล้วใช้ add_transaction เพื่อเก็บรายละเอียดที่สมบูรณ์ ไม่ทิ้งบริบทเดิม "
        "เมื่อผู้ใช้ถามยอดหรือรายการ ให้ query ฐานข้อมูลทุกครั้ง และนำเสนอให้อ่านง่าย แยก วันนี้/รับ/จ่าย/สุทธิ/รวม หรือจัดรายการตามวันที่ "
        "รองรับงบรายหมวด เป้าหมายเงินออม การวิเคราะห์ และคำเตือนการเงิน"
    ),
)

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def db() -> AdvancedFinanceDB:
    return AdvancedFinanceDB()


@mcp.tool(
    title="ตรวจรายการก่อนบันทึก",
    description="Use this for short, unclear, or abbreviated finance text. It researches the entry using deterministic parsing plus prior transaction history and returns clarification questions without writing anything.",
    annotations=READ,
)
def analyze_entry(text: str) -> dict:
    """Preview interpretation and determine whether the user should be asked for more detail."""
    return db().analyze_entry(text)


@mcp.tool(
    title="บันทึกรายการแบบข้อความ",
    description="Use this for natural-language finance entries. Clear entries are saved; ambiguous/too-short entries are NOT saved and return questions. Set confirm_ambiguous only after the user explicitly says to save despite missing details.",
    annotations=WRITE,
)
def record_entry(text: str, allow_duplicate: bool = False, confirm_ambiguous: bool = False) -> dict:
    """Parse and save only when sufficiently clear; otherwise ask before writing."""
    d = db()
    assessment = d.analyze_entry(text)
    if assessment["needs_clarification"] and not confirm_ambiguous:
        return {
            "saved": False,
            "needs_clarification": True,
            "confidence": assessment["confidence"],
            "questions": assessment["questions"],
            "reasons": assessment["reasons"],
            "preview": assessment["suggested"],
            "learned_from_history": assessment.get("learned_from_history"),
        }
    p = parse_simple_entry(text)
    suggested = assessment["suggested"]
    result = d.add_transaction(
        kind=suggested["kind"],
        amount=p.amount,
        description=p.description,
        category=suggested["category"],
        occurred_at=p.occurred_at,
        raw_input=p.raw_input,
        allow_duplicate=allow_duplicate,
    )
    result["saved"] = True
    result["needs_clarification"] = False
    result["confidence"] = assessment["confidence"]
    result["parsed_as"] = suggested
    result["learned_from_history"] = assessment.get("learned_from_history")
    return result


@mcp.tool(
    title="เพิ่มรายรับหรือรายจ่าย",
    description="Use this when the amount/type/date/category are known and should be stored exactly rather than inferred from a short text entry.",
    annotations=WRITE,
)
def add_transaction(
    kind: Literal["income", "expense"],
    amount: str,
    description: str,
    category: str = "other",
    occurred_at: str | None = None,
    payment_method: str | None = None,
    note: str | None = None,
    account: str | None = None,
    idempotency_key: str | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Create one transaction. Amount is a positive decimal string, e.g. '75' or '199.50'."""
    return db().add_transaction(
        kind=kind,
        amount=amount,
        description=description,
        category=category,
        occurred_at=occurred_at,
        payment_method=payment_method,
        note=note,
        account=account,
        idempotency_key=idempotency_key,
        allow_duplicate=allow_duplicate,
    )


@mcp.tool(
    title="ดูรายการ",
    description="Use this when the user asks what they spent/earned, requests recent transactions, or wants filtering by date/type/category/text.",
    annotations=READ,
)
def list_transactions(
    start_date: str | None = None,
    end_date: str | None = None,
    kind: Literal["income", "expense"] | None = None,
    category: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List active transactions newest first."""
    return db().list_transactions(
        start_date=start_date,
        end_date=end_date,
        kind=kind,
        category=category,
        query=query,
        limit=limit,
    )


@mcp.tool(
    title="ดูสมุดบัญชีแบบอ่านง่าย",
    description="Use this when the user says list รายการบัญชี, show ledger, or wants a readable transaction history grouped by date with income, expense, net, and current total balance.",
    annotations=READ,
)
def get_ledger_view(
    start_date: str | None = None,
    end_date: str | None = None,
    kind: Literal["income", "expense"] | None = None,
    category: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict:
    d = db()
    rows = d.list_transactions(start_date=start_date, end_date=end_date, kind=kind, category=category, query=query, limit=limit)
    accounts = d.list_accounts()
    current = next((a for a in accounts if a["name"] == "เงินปัจจุบัน"), accounts[0] if accounts else None)
    balance = current["balance"] if current else None
    return {
        "transactions": rows,
        "transaction_count": len(rows),
        "current_balance": balance,
        "currency": "THB",
        "text": format_transactions(rows, balance=balance),
    }


@mcp.tool(
    title="ดูรายการตาม ID",
    description="Use this when an exact transaction ID is known and the complete stored record is needed.",
    annotations=READ,
)
def get_transaction(transaction_id: str) -> dict:
    """Fetch one transaction by UUID."""
    item = db().get_transaction(transaction_id)
    if item is None:
        return {"found": False, "transaction_id": transaction_id}
    return {"found": True, "transaction": item}


@mcp.tool(
    title="สรุปรายรับรายจ่าย",
    description="Use this whenever the user asks totals, net balance, spending by category, today's/monthly expenses, or income-vs-expense.",
    annotations=READ,
)
def get_summary(
    period: Literal["today", "yesterday", "this_week", "this_month", "last_month", "custom"] = "this_month",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Calculate deterministic totals from SQLite. For custom period provide YYYY-MM-DD start/end dates."""
    d = db()
    result = d.summary(period, start_date, end_date)
    accounts = d.list_accounts()
    current = next((a for a in accounts if a["name"] == "เงินปัจจุบัน"), accounts[0] if accounts else None)
    balance = current["balance"] if current else None
    result["current_balance"] = balance
    result["text"] = format_summary(result, balance=balance)
    return result


@mcp.tool(
    title="เทียบรายเดือน",
    description="Use this when the user asks how spending/income changed over recent months or which month cost the most.",
    annotations=READ,
)
def compare_months(months: int = 3) -> list[dict]:
    """Compare up to 24 recent calendar months."""
    return db().compare_months(months)


@mcp.tool(
    title="แก้รายการ",
    description="Use this when the user corrects a previously stored transaction. Pass only fields that should change.",
    annotations=WRITE,
)
def update_transaction(
    transaction_id: str,
    kind: Literal["income", "expense"] | None = None,
    amount: str | None = None,
    description: str | None = None,
    category: str | None = None,
    occurred_at: str | None = None,
    payment_method: str | None = None,
    note: str | None = None,
) -> dict:
    """Update a transaction and preserve an audit trail."""
    changes = {
        "kind": kind,
        "amount": amount,
        "description": description,
        "category": category,
        "occurred_at": occurred_at,
        "payment_method": payment_method,
        "note": note,
    }
    return db().update_transaction(transaction_id, **changes)


@mcp.tool(
    title="ลบรายการ",
    description="Use this only when the user explicitly wants a specific transaction removed. This is a soft delete with audit history.",
    annotations=DELETE,
)
def delete_transaction(transaction_id: str, reason: str = "user requested") -> dict:
    """Soft-delete one transaction; audit data is retained."""
    return db().delete_transaction(transaction_id, reason)


@mcp.tool(
    title="ย้อนรายการล่าสุด",
    description="Use this when the user explicitly says the most recently added finance entry was wrong and wants it undone.",
    annotations=DELETE,
)
def undo_last_transaction(reason: str = "user requested undo") -> dict:
    """Soft-delete the most recently created active transaction."""
    return db().undo_last(reason)


@mcp.tool(
    title="สถานะระบบบัญชี",
    description="Use this to verify that the finance database is reachable and the MCP service is working.",
    annotations=READ,
)
def finance_health() -> dict:
    """Return non-secret health information."""
    d = db()
    summary = d.summary("today")
    return {
        "ok": True,
        "database": str(d.path),
        "today_transaction_count": summary["transaction_count"],
        "currency": summary["currency"],
        "timezone": "Asia/Bangkok",
    }


@mcp.tool(title="ภาพรวมการเงิน", description="Use this for questions like เก็บได้กี่บาท, เหลือเท่าไร, ใช้เฉลี่ยวันละเท่าไร, savings rate, or a compact financial dashboard.", annotations=READ)
def get_financial_overview(period: Literal["today","yesterday","this_week","this_month","last_month","custom"]="this_month", start_date: str|None=None, end_date: str|None=None) -> dict:
    return db().financial_overview(period,start_date,end_date)

@mcp.tool(title="ตั้งงบรายหมวด", description="Set or update a monthly spending budget for a category and warning threshold.", annotations=WRITE)
def set_budget(category: str, amount: str, warning_percent: int=80) -> dict:
    return db().set_budget(category,amount,warning_percent)

@mcp.tool(title="ดูงบทั้งหมด", description="List configured monthly budgets.", annotations=READ)
def list_budgets() -> list[dict]:
    return db().list_budgets()

@mcp.tool(title="สถานะงบ", description="Check monthly budget usage and remaining amount, optionally for one category.", annotations=READ)
def get_budget_status(category: str|None=None) -> dict:
    return db().budget_status(category)

@mcp.tool(title="ตั้งเป้าหมายเงินออม", description="Create or update a savings goal with target amount/date and optional current saved amount.", annotations=WRITE)
def set_savings_goal(name: str, target: str, target_date: str|None=None, saved: str|None=None) -> dict:
    return db().set_savings_goal(name,target,target_date,saved)

@mcp.tool(title="ดูเป้าหมายเงินออม", description="List savings goals and progress.", annotations=READ)
def get_savings_goals(name: str|None=None) -> list[dict]:
    return db().get_savings_goals(name)

@mcp.tool(title="เพิ่มเงินเข้าเป้าหมายออม", description="Add saved money to an existing savings goal.", annotations=WRITE)
def add_savings(name: str, amount: str) -> dict:
    return db().add_savings(name,amount)

@mcp.tool(title="คำเตือนการเงิน", description="Check for budget warnings, budget overruns, or negative monthly cash flow.", annotations=READ)
def get_financial_alerts() -> list[dict]:
    return db().financial_alerts()


@mcp.tool(title="สร้าง/แก้บัญชี", description="Create or update a cash/bank/wallet/savings/investment account with an opening balance.", annotations=WRITE)
def create_account(name: str, account_type: Literal["cash","bank","wallet","savings","investment","other"]="bank", opening_balance: str="0") -> dict:
    return db().create_account(name, account_type, opening_balance)

@mcp.tool(title="ดูบัญชีและยอดคงเหลือ", description="List money accounts and calculated balances including transfers and linked transactions.", annotations=READ)
def list_accounts(active_only: bool=True) -> list[dict]:
    return db().list_accounts(active_only)

@mcp.tool(title="โอนเงินระหว่างบัญชี", description="Transfer money between two tracked accounts without affecting income/expense totals.", annotations=WRITE)
def transfer_accounts(from_account: str, to_account: str, amount: str, occurred_at: str|None=None, note: str|None=None) -> dict:
    return db().transfer(from_account,to_account,amount,occurred_at,note)

@mcp.tool(title="ตั้งรายการประจำ", description="Create or update a recurring income/expense item such as salary, rent, hosting, allowance, or utilities.", annotations=WRITE)
def set_recurring(name: str, kind: Literal["income","expense"], amount: str, description: str, category: str="other", cadence: Literal["daily","weekly","monthly","yearly"]="monthly", next_due_date: str="วันนี้", account: str|None=None, payment_method: str|None=None) -> dict:
    return db().set_recurring(name,kind,amount,description,category,cadence,next_due_date,account,payment_method)

@mcp.tool(title="ดูรายการประจำ", description="List recurring income/expense schedules and their next due dates.", annotations=READ)
def list_recurring(name: str|None=None, enabled_only: bool=False) -> list[dict]:
    return db().get_recurring(name,enabled_only)

@mcp.tool(title="ลงรายการประจำที่ถึงกำหนด", description="Post due recurring items into transactions through a date. Uses idempotency keys to prevent duplicate scheduled postings.", annotations=WRITE)
def process_due_recurring(through_date: str|None=None) -> dict:
    return db().process_due_recurring(through_date)

@mcp.tool(title="ตั้ง Subscription", description="Create or update a subscription tracker with amount, billing cycle, and next charge date.", annotations=WRITE)
def set_subscription(name: str, amount: str, cadence: Literal["weekly","monthly","yearly"], next_due_date: str, category: str="software", account: str|None=None, note: str|None=None) -> dict:
    return db().set_subscription(name,amount,cadence,next_due_date,category,account,note)

@mcp.tool(title="ดู Subscription", description="List active subscriptions, amounts, and days until the next charge.", annotations=READ)
def list_subscriptions(name: str|None=None, enabled_only: bool=True) -> list[dict]:
    return db().list_subscriptions(name,enabled_only)

@mcp.tool(title="ตั้งหนี้หรือลูกหนี้", description="Create or update money you owe (payable) or money others owe you (receivable).", annotations=WRITE)
def set_debt(name: str, kind: Literal["payable","receivable"], amount: str, due_date: str|None=None, note: str|None=None) -> dict:
    return db().set_debt(name,kind,amount,due_date,note)

@mcp.tool(title="ดูหนี้และลูกหนี้", description="List debt/receivable balances, repayment progress, and due dates.", annotations=READ)
def list_debts(name: str|None=None, open_only: bool=True) -> list[dict]:
    return db().list_debts(name,open_only)

@mcp.tool(title="ชำระหนี้/รับชำระลูกหนี้", description="Reduce an existing debt balance and optionally record the matching transaction.", annotations=WRITE)
def pay_debt(name: str, amount: str, account: str|None=None, record_transaction: bool=True) -> dict:
    return db().pay_debt(name,amount,account,record_transaction)

@mcp.tool(title="พยากรณ์สิ้นเดือน", description="Forecast month-end cash flow, scheduled expenses/income, and safe daily allowance for a target savings amount.", annotations=READ)
def get_month_forecast(target_savings: str="0") -> dict:
    return db().month_forecast(target_savings)

@mcp.tool(title="ตรวจรายจ่ายผิดปกติ", description="Detect unusually large expenses compared with historical spending in the same category.", annotations=READ)
def detect_spending_anomalies(days: int=90, min_samples: int=3) -> list[dict]:
    return db().anomalies(days,min_samples)

@mcp.tool(title="มูลค่าสุทธิ", description="Calculate net worth from tracked account balances plus receivables minus payables.", annotations=READ)
def get_net_worth() -> dict:
    return db().net_worth()

@mcp.tool(title="ศูนย์แจ้งเตือนการเงิน", description="Return combined budget, cash-flow, subscription due, and debt due/overdue alerts.", annotations=READ)
def get_advanced_financial_alerts(upcoming_days: int=7) -> list[dict]:
    return db().advanced_alerts(upcoming_days)


def main() -> None:
    mcp.run()  # stdio; tunnel-client launches this command


if __name__ == "__main__":
    main()
