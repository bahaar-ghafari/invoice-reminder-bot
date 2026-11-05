import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Optional

from domain import next_occurrence


class Database:
    def __init__(self, path: Optional[str] = None):
        configured_path = path or os.environ.get("DATABASE_PATH")
        if configured_path:
            self.path = (
                configured_path
                if os.path.isabs(configured_path)
                else os.path.join(os.path.dirname(os.path.abspath(__file__)), configured_path)
            )
        else:
            self.path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "data",
                "invoices.sqlite3",
            )
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    reminder_offsets TEXT NOT NULL DEFAULT '[3,0]',
                    reminder_time TEXT NOT NULL DEFAULT '09:00',
                    default_currency TEXT DEFAULT 'EUR'
                );
                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    title TEXT NOT NULL,
                    amount_minor INTEGER,
                    currency TEXT,
                    next_due TEXT,
                    anchor_day INTEGER,
                    frequency TEXT NOT NULL DEFAULT 'once',
                    interval_days INTEGER,
                    end_after_count INTEGER,
                    end_date TEXT,
                    payments_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    reminder_offsets TEXT NOT NULL DEFAULT '[3,0]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    due_date TEXT,
                    payment_date TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
                    due_date TEXT NOT NULL,
                    offset_days INTEGER NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (invoice_id, due_date, offset_days)
                );
                CREATE TABLE IF NOT EXISTS snoozes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    due_date TEXT NOT NULL,
                    remind_on TEXT NOT NULL,
                    sent_at TEXT,
                    UNIQUE (invoice_id, due_date)
                );
                """
            )
            user_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            }
            if "default_currency" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN default_currency TEXT DEFAULT 'EUR'"
                )
            invoice_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(invoices)")
            }
            if "anchor_day" not in invoice_columns:
                conn.execute("ALTER TABLE invoices ADD COLUMN anchor_day INTEGER")
            conn.execute(
                """
                UPDATE invoices
                SET anchor_day = CAST(substr(next_due, 9, 2) AS INTEGER)
                WHERE anchor_day IS NULL AND next_due IS NOT NULL
                """
            )

    def ensure_user(self, user_id: int):
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

    def get_user(self, user_id: int):
        self.ensure_user(user_id)
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

    def update_user(self, user_id: int, **fields):
        allowed = {"timezone", "reminder_offsets", "reminder_time", "default_currency"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Invalid user settings.")
        self.ensure_user(user_id)
        assignments = ", ".join("{} = ?".format(key) for key in fields)
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET {} WHERE user_id = ?".format(assignments),
                tuple(fields.values()) + (user_id,),
            )
            if "reminder_offsets" in fields:
                conn.execute(
                    "UPDATE invoices SET reminder_offsets = ? WHERE user_id = ?",
                    (fields["reminder_offsets"], user_id),
                )

    def add_invoice(
        self,
        user_id: int,
        title: str,
        amount_minor: Optional[int],
        currency: Optional[str],
        next_due: Optional[str],
        frequency: str,
        interval_days: Optional[int],
        end_after_count: Optional[int],
        end_date: Optional[str],
        reminder_offsets,
    ) -> int:
        self.ensure_user(user_id)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO invoices
                    (user_id, title, amount_minor, currency, next_due, anchor_day, frequency,
                     interval_days, end_after_count, end_date, reminder_offsets)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    title,
                    amount_minor,
                    currency,
                    next_due,
                    date.fromisoformat(next_due).day if next_due else None,
                    frequency,
                    interval_days,
                    end_after_count,
                    end_date,
                    json.dumps(reminder_offsets),
                ),
            )
            return cursor.lastrowid

    def get_invoice(self, invoice_id: int, user_id: int):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM invoices WHERE id = ? AND user_id = ?",
                (invoice_id, user_id),
            ).fetchone()

    def list_invoices(self, user_id: int, active_only: bool = False):
        sql = "SELECT * FROM invoices WHERE user_id = ?"
        args = [user_id]
        if active_only:
            sql += " AND status = 'active'"
        sql += " ORDER BY CASE WHEN next_due IS NULL THEN 1 ELSE 0 END, next_due, id"
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def list_all_active_invoices(self):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM invoices
                WHERE status = 'active'
                  AND (end_date IS NULL OR next_due IS NULL OR next_due <= end_date)
                ORDER BY user_id, next_due, id
                """
            ).fetchall()

    def update_invoice(self, invoice_id: int, user_id: int, **fields):
        allowed = {
            "title",
            "amount_minor",
            "currency",
            "next_due",
            "anchor_day",
            "frequency",
            "interval_days",
            "end_after_count",
            "end_date",
            "reminder_offsets",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Invalid invoice fields.")
        if "next_due" in fields and "anchor_day" not in fields:
            fields["anchor_day"] = (
                date.fromisoformat(fields["next_due"]).day
                if fields["next_due"]
                else None
            )
        if "reminder_offsets" in fields and not isinstance(fields["reminder_offsets"], str):
            fields["reminder_offsets"] = json.dumps(fields["reminder_offsets"])
        assignments = ", ".join("{} = ?".format(key) for key in fields)
        with self.connect() as conn:
            conn.execute(
                "UPDATE invoices SET {} WHERE id = ? AND user_id = ?".format(assignments),
                tuple(fields.values()) + (invoice_id, user_id),
            )

    def set_status(self, invoice_id: int, user_id: int, status: str):
        if status not in {"active", "paused"}:
            raise ValueError("Invalid invoice status.")
        with self.connect() as conn:
            conn.execute(
                "UPDATE invoices SET status = ? WHERE id = ? AND user_id = ?",
                (status, invoice_id, user_id),
            )

    def payment_total(self, invoice_id: int, due_date: Optional[str]):
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount_minor), 0) AS total FROM payments
                WHERE invoice_id = ? AND due_date IS ?
                """,
                (invoice_id, due_date),
            ).fetchone()
            return row["total"]

    def record_payment(
        self,
        invoice,
        amount_minor: int,
        payment_date: str,
        due_date: Optional[str] = None,
    ):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO payments
                    (invoice_id, user_id, due_date, payment_date, amount_minor, currency)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice["id"],
                    invoice["user_id"],
                    due_date if due_date is not None else invoice["next_due"],
                    payment_date,
                    amount_minor,
                    invoice["currency"],
                ),
            )

    def complete_installment(self, invoice_id: int, user_id: int, payment_date: str):
        with self.connect() as conn:
            invoice = conn.execute(
                "SELECT * FROM invoices WHERE id = ? AND user_id = ?",
                (invoice_id, user_id),
            ).fetchone()
            if invoice is None or invoice["status"] != "active":
                return "unavailable"
            due_date = invoice["next_due"]
            if invoice["amount_minor"] is not None:
                total = conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_minor), 0) AS total FROM payments
                    WHERE invoice_id = ? AND due_date IS ?
                    """,
                    (invoice_id, due_date),
                ).fetchone()["total"]
                remaining = max(0, invoice["amount_minor"] - total)
                if remaining:
                    conn.execute(
                        """
                        INSERT INTO payments
                            (invoice_id, user_id, due_date, payment_date, amount_minor, currency)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invoice_id,
                            user_id,
                            due_date,
                            payment_date,
                            remaining,
                            invoice["currency"],
                        ),
                    )
            count = invoice["payments_count"] + 1
            frequency = invoice["frequency"]
            next_due = None
            if due_date:
                next_due = next_occurrence(
                    date.fromisoformat(due_date),
                    frequency,
                    invoice["interval_days"],
                    invoice["anchor_day"],
                )
            completed = frequency == "once"
            completed = completed or (
                invoice["end_after_count"] is not None
                and count >= invoice["end_after_count"]
            )
            completed = completed or (
                next_due is not None
                and invoice["end_date"] is not None
                and next_due > date.fromisoformat(invoice["end_date"])
            )
            if completed or next_due is None:
                status = "completed"
                next_due_value = due_date
            else:
                status = "active"
                next_due_value = next_due.isoformat()
            conn.execute(
                """
                UPDATE invoices
                SET payments_count = ?, status = ?, next_due = ?
                WHERE id = ? AND user_id = ?
                """,
                (count, status, next_due_value, invoice_id, user_id),
            )
            return "completed" if status == "completed" else "advanced"

    def add_snooze(self, invoice, remind_on: str):
        if invoice["next_due"] is None:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO snoozes (invoice_id, user_id, due_date, remind_on, sent_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(invoice_id, due_date)
                DO UPDATE SET remind_on = excluded.remind_on, sent_at = NULL
                """,
                (invoice["id"], invoice["user_id"], invoice["next_due"], remind_on),
            )

    def has_notification(self, invoice_id: int, due_date: str, offset_days: int):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE invoice_id = ? AND due_date = ? AND offset_days = ?
                """,
                (invoice_id, due_date, offset_days),
            ).fetchone() is not None

    def mark_notification_sent(self, invoice_id: int, due_date: str, offset_days: int):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notifications (invoice_id, due_date, offset_days)
                VALUES (?, ?, ?)
                """,
                (invoice_id, due_date, offset_days),
            )

    def due_snoozes(self, on_date: str):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT s.*, i.title, i.status FROM snoozes s
                JOIN invoices i ON i.id = s.invoice_id
                WHERE s.remind_on <= ? AND s.sent_at IS NULL
                  AND i.status = 'active' AND i.next_due = s.due_date
                """,
                (on_date,),
            ).fetchall()

    def mark_snooze_sent(self, snooze_id: int):
        with self.connect() as conn:
            conn.execute(
                "UPDATE snoozes SET sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                (snooze_id,),
            )

    def list_payments(self, user_id: int, limit: int = 50):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT p.*, i.title FROM payments p
                JOIN invoices i ON i.id = p.invoice_id
                WHERE p.user_id = ?
                ORDER BY p.payment_date DESC, p.id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
