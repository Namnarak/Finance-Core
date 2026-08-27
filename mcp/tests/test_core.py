from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finance_mcp.core import FinanceDB, money_to_minor, parse_simple_entry


class FinanceCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = FinanceDB(Path(self.tmp.name) / "finance.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_money(self):
        self.assertEqual(money_to_minor("1,234.56"), 123456)

    def test_parse_short_expense(self):
        p = parse_simple_entry("ข้าว 75")
        self.assertEqual(p.kind, "expense")
        self.assertEqual(p.amount, "75")
        self.assertEqual(p.category, "food")

    def test_parse_income(self):
        p = parse_simple_entry("รับค่าทำเว็บ 1500")
        self.assertEqual(p.kind, "income")
        self.assertEqual(p.amount, "1500")

    def test_analyze_clear_short_food(self):
        a = self.db.analyze_entry("ข้าว 75")
        self.assertFalse(a["needs_clarification"])
        self.assertEqual(a["suggested"]["category"], "food")

    def test_analyze_merchant_only_requires_detail(self):
        a = self.db.analyze_entry("สหกรณ์ 30")
        self.assertTrue(a["needs_clarification"])
        self.assertTrue(any("ซื้ออะไร" in q for q in a["questions"]))

    def test_analyze_number_only_requires_kind_and_detail(self):
        a = self.db.analyze_entry("30")
        self.assertTrue(a["needs_clarification"])
        self.assertGreaterEqual(len(a["questions"]), 1)

    def test_history_can_supply_category_for_specific_entry(self):
        self.db.add_transaction(kind="expense", amount="20", description="PR Big Pack", category="food", occurred_at="วันนี้")
        a = self.db.analyze_entry("PR Big Pack 25")
        self.assertFalse(a["needs_clarification"])
        self.assertEqual(a["suggested"]["category"], "food")
        self.assertIsNotNone(a["learned_from_history"])

    def test_add_and_summary(self):
        self.db.add_transaction(kind="income", amount="1000", description="งาน", occurred_at="วันนี้")
        self.db.add_transaction(kind="expense", amount="250.50", description="อาหาร", category="food", occurred_at="วันนี้")
        s = self.db.summary("today")
        self.assertEqual(s["income"], "1,000.00")
        self.assertEqual(s["expense"], "250.50")
        self.assertEqual(s["net"], "749.50")

    def test_soft_delete(self):
        tx = self.db.add_transaction(kind="expense", amount="50", description="น้ำ", occurred_at="วันนี้")
        self.db.delete_transaction(tx["id"], "test")
        self.assertIsNone(self.db.get_transaction(tx["id"]))
        self.assertEqual(self.db.summary("today")["expense"], "0.00")

    def test_duplicate_guard(self):
        a = self.db.add_transaction(kind="expense", amount="75", description="ข้าว", occurred_at="วันนี้")
        b = self.db.add_transaction(kind="expense", amount="75", description="ข้าว", occurred_at="วันนี้")
        self.assertFalse(a["duplicate_prevented"])
        self.assertTrue(b["duplicate_prevented"])
        self.assertEqual(a["id"], b["id"])


if __name__ == "__main__":
    unittest.main()
