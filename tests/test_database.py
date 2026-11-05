import os
import tempfile
import unittest

from database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = Database(self.path)
        self.db.ensure_user(1001)

    def tearDown(self):
        os.unlink(self.path)

    def add_invoice(self, **overrides):
        values = {
            "user_id": 1001,
            "title": "Rent",
            "amount_minor": 100000,
            "currency": "EUR",
            "next_due": "2026-01-31",
            "frequency": "monthly",
            "interval_days": None,
            "end_after_count": None,
            "end_date": None,
            "reminder_offsets": [3, 0],
        }
        values.update(overrides)
        return self.db.add_invoice(**values)

    def test_payment_history_is_private_to_the_owner(self):
        invoice_id = self.add_invoice()
        invoice = self.db.get_invoice(invoice_id, 1001)
        self.db.record_payment(invoice, 2500, "2026-01-10")
        self.assertEqual(self.db.payment_total(invoice_id, "2026-01-31"), 2500)
        self.assertIsNone(self.db.get_invoice(invoice_id, 1002))
        self.assertEqual(len(self.db.list_payments(1001)), 1)
        self.assertEqual(self.db.list_payments(1002), [])

    def test_completing_installment_records_remainder_and_advances_schedule(self):
        invoice_id = self.add_invoice()
        invoice = self.db.get_invoice(invoice_id, 1001)
        self.db.record_payment(invoice, 2500, "2026-01-10")
        result = self.db.complete_installment(invoice_id, 1001, "2026-01-31")
        updated = self.db.get_invoice(invoice_id, 1001)
        self.assertEqual(result, "advanced")
        self.assertEqual(updated["next_due"], "2026-02-28")
        self.assertEqual(updated["payments_count"], 1)
        self.assertEqual(self.db.payment_total(invoice_id, "2026-01-31"), 100000)
        self.assertEqual(
            self.db.complete_installment(invoice_id, 1001, "2026-02-28"),
            "advanced",
        )
        self.assertEqual(
            self.db.get_invoice(invoice_id, 1001)["next_due"],
            "2026-03-31",
        )

    def test_count_limit_completes_after_requested_installments(self):
        invoice_id = self.add_invoice(end_after_count=1)
        self.assertEqual(self.db.complete_installment(invoice_id, 1001, "2026-01-31"), "completed")
        invoice = self.db.get_invoice(invoice_id, 1001)
        self.assertEqual(invoice["status"], "completed")

    def test_stop_and_resume_only_change_owned_invoice(self):
        invoice_id = self.add_invoice()
        self.db.set_status(invoice_id, 1001, "paused")
        self.assertEqual(self.db.get_invoice(invoice_id, 1001)["status"], "paused")
        self.db.set_status(invoice_id, 1001, "active")
        self.assertEqual(self.db.get_invoice(invoice_id, 1001)["status"], "active")

    def test_global_reminder_preference_updates_existing_invoices(self):
        invoice_id = self.add_invoice()
        self.db.update_user(1001, reminder_offsets="[1,0]")
        self.assertEqual(
            self.db.get_invoice(invoice_id, 1001)["reminder_offsets"],
            "[1,0]",
        )

    def test_default_currency_can_be_changed_or_cleared(self):
        self.assertEqual(self.db.get_user(1001)["default_currency"], "EUR")
        self.db.update_user(1001, default_currency="USD")
        self.assertEqual(self.db.get_user(1001)["default_currency"], "USD")
        self.db.update_user(1001, default_currency=None)
        self.assertIsNone(self.db.get_user(1001)["default_currency"])

    def test_snooze_is_not_sent_after_the_invoice_has_advanced(self):
        invoice_id = self.add_invoice()
        invoice = self.db.get_invoice(invoice_id, 1001)
        self.db.add_snooze(invoice, "2026-02-01")
        self.db.complete_installment(invoice_id, 1001, "2026-01-31")
        self.assertEqual(self.db.due_snoozes("2026-02-01"), [])


if __name__ == "__main__":
    unittest.main()
