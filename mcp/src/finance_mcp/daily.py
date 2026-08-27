from __future__ import annotations

import argparse
from datetime import timedelta

from .config import DB_PATH, REPORT_DIR
from .advanced import AdvancedFinanceDB
from .core import format_summary, now_local
from .notify import send_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and optionally deliver a daily finance summary")
    parser.add_argument("--date", help="YYYY-MM-DD; default is yesterday")
    parser.add_argument("--no-send", action="store_true", help="Only generate report file/stdout")
    args = parser.parse_args()

    target = args.date or (now_local().date() - timedelta(days=1)).isoformat()
    db = AdvancedFinanceDB()
    recurring = db.process_due_recurring(target)
    summary = db.summary("custom", target, target)
    text = format_summary(summary)
    alerts = db.advanced_alerts(7)
    if recurring["posted_count"]:
        text += f"\n\n🔁 ลงรายการประจำอัตโนมัติ: {recurring['posted_count']} รายการ"
    if alerts:
        text += "\n\n⚠️ แจ้งเตือนการเงิน\n" + "\n".join(f"- {a.get('message', a.get('type', 'alert'))}" for a in alerts[:10])

    # Online SQLite backup (safe with WAL) and 30-day local retention.
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"finance-{target}.sqlite3"
    db.backup(backup_path)
    backups = sorted(backup_dir.glob("finance-*.sqlite3"), reverse=True)
    for old in backups[30:]:
        old.unlink(missing_ok=True)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{target}.txt"
    report_path.write_text(text + "\n", encoding="utf-8")

    delivered: list[str] = []
    if not args.no_send:
        delivered = send_summary(text)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO summary_runs(summary_date,generated_at,delivered_to) VALUES(?,?,?) "
            "ON CONFLICT(summary_date) DO UPDATE SET generated_at=excluded.generated_at, delivered_to=excluded.delivered_to",
            (target, now_local().isoformat(timespec="seconds"), ",".join(delivered) if delivered else "report-file"),
        )

    print(text)
    print(f"\nreport={report_path}")
    print(f"backup={backup_path}")
    print(f"delivered_to={','.join(delivered) if delivered else 'report-file'}")


if __name__ == "__main__":
    main()
