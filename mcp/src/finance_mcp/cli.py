from __future__ import annotations

import argparse
import json
from datetime import datetime

from .config import DB_PATH, REPORT_DIR
from .core import FinanceDB, format_summary, parse_simple_entry


def dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="finance", description="NamKrub Finance local CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="บันทึกข้อความสั้น เช่น finance add 'ข้าว 75'")
    p_add.add_argument("text", nargs="+")
    p_add.add_argument("--allow-duplicate", action="store_true")

    p_sum = sub.add_parser("summary", help="สรุปยอด")
    p_sum.add_argument("period", nargs="?", default="this_month", choices=["today","yesterday","this_week","this_month","last_month"])

    p_list = sub.add_parser("list", help="ดูรายการล่าสุด")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--query")

    p_del = sub.add_parser("delete", help="soft-delete transaction")
    p_del.add_argument("id")
    p_del.add_argument("--reason", default="cli delete")

    sub.add_parser("undo", help="ย้อนรายการล่าสุด")
    sub.add_parser("doctor", help="ตรวจสุขภาพระบบ")

    args = parser.parse_args()
    db = FinanceDB()

    if args.cmd == "add":
        text = " ".join(args.text)
        p = parse_simple_entry(text)
        dump(db.add_transaction(
            kind=p.kind, amount=p.amount, description=p.description, category=p.category,
            occurred_at=p.occurred_at, raw_input=p.raw_input, source="cli", allow_duplicate=args.allow_duplicate,
        ))
    elif args.cmd == "summary":
        print(format_summary(db.summary(args.period)))
    elif args.cmd == "list":
        dump(db.list_transactions(query=args.query, limit=args.limit))
    elif args.cmd == "delete":
        dump(db.delete_transaction(args.id, args.reason))
    elif args.cmd == "undo":
        dump(db.undo_last("cli undo"))
    elif args.cmd == "doctor":
        s = db.summary("today")
        dump({
            "ok": True,
            "database": str(DB_PATH),
            "database_exists": DB_PATH.exists(),
            "report_dir": str(REPORT_DIR),
            "today_transactions": s["transaction_count"],
            "currency": s["currency"],
        })


if __name__ == "__main__":
    main()
