import unittest
from datetime import date
from domain import (
    InputError,
    add_months,
    format_money,
    next_occurrence,
    parse_date,
    parse_money,
    parse_offsets,
    reminder_offsets_to_send,
)


class DomainTests(unittest.TestCase):
    def test_money_parsing_uses_minor_units_and_optional_currency(self):
        self.assertEqual(parse_money("49.995 eur"), (5000, "EUR"))
        self.assertEqual(parse_money("12"), (1200, None))
        self.assertEqual(format_money(1234, "USD"), "12.34 USD")
        self.assertEqual(format_money(None, None), "not provided")

    def test_invalid_money_is_rejected(self):
        for value in ("-1", "abc", "1.00 US", "1 2 3"):
            with self.subTest(value=value), self.assertRaises(InputError):
                parse_money(value)

    def test_dates_and_reminder_offsets_are_validated(self):
        self.assertEqual(parse_date("2026-02-01"), date(2026, 2, 1))
        self.assertEqual(parse_offsets("3,0,3"), [0, 3])
        with self.assertRaises(InputError):
            parse_date("01-02-2026")
        with self.assertRaises(InputError):
            parse_offsets("-1,400")

    def test_monthly_and_yearly_schedules_clamp_to_month_end(self):
        self.assertEqual(
            add_months(date(2026, 1, 31), 1),
            date(2026, 2, 28),
        )
        self.assertEqual(
            next_occurrence(date(2024, 2, 29), "yearly"),
            date(2025, 2, 28),
        )
        self.assertEqual(
            next_occurrence(date(2026, 2, 28), "monthly", anchor_day=31),
            date(2026, 3, 31),
        )
        due = date(2025, 2, 28)
        for _ in range(3):
            due = next_occurrence(due, "yearly", anchor_day=29)
        self.assertEqual(due, date(2028, 2, 29))

    def test_custom_and_one_time_recurrence(self):
        self.assertEqual(
            next_occurrence(date(2026, 1, 1), "custom", 14),
            date(2026, 1, 15),
        )
        self.assertIsNone(next_occurrence(date(2026, 1, 1), "once"))

    def test_reminders_catch_up_without_sending_old_lead_and_due_notices_together(self):
        due = date(2026, 5, 10)
        self.assertEqual(
            reminder_offsets_to_send(due, date(2026, 5, 7), [3, 0]),
            [3],
        )
        self.assertEqual(
            reminder_offsets_to_send(due, date(2026, 5, 9), [3, 0]),
            [3],
        )
        self.assertEqual(
            reminder_offsets_to_send(due, due, [3, 0]),
            [0],
        )
        self.assertEqual(
            reminder_offsets_to_send(due, date(2026, 5, 11), [3, 0]),
            [0],
        )


if __name__ == "__main__":
    unittest.main()
