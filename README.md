# Invoice Reminder Telegram Bot

A private-chat Telegram bot for recurring bills, reminders, and payment history. It stores data in a local SQLite database and supports optional invoice details, partial payments, timezones, and reminder controls.

## Features

- Guided `/add` setup: name, amount, currency, next due date, recurrence, stop condition, and already-paid amount. Optional answers can be skipped.
- One-time, weekly, monthly, quarterly, yearly, and custom day intervals.
- Recurrence can stop after a number of payments, on a date, or continue until paused.
- Reminders on the due date and/or a chosen number of days before it, at a chosen local time.
- Inline actions to mark paid, record a partial payment, snooze for a day, edit, pause, or resume.
- A dated payment ledger, remaining current-installment balance, `/history`, `/list`, and `/upcoming`.
- Each Telegram user can access only their own invoice and payment records.

## Run locally

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Use Python 3.9 or later and install dependencies:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`.
4. Start the bot:

   ```sh
   python bot.py
   ```

Keep the process running for scheduled reminders. The database is created at `data/invoices.sqlite3` by default. Back up that file to preserve invoice and payment history.

## Commands

- `/start` and `/help` — instructions
- `/add` — create an invoice/reminder
- `/list` — view and manage invoices
- `/upcoming` — overdue and next 30 days
- `/history` — recent payment records (up to 50)
- `/settings` — timezone, reminder lead days, and time
- `/cancel` — cancel an active setup

Dates use `YYYY-MM-DD`; times use 24-hour `HH:MM`. Reminder lead days such as `3,0` means three days before and on the due date. Changing reminder lead days in `/settings` updates all existing invoices; you can then customize individual invoices from `/list`. Use an IANA timezone such as `Europe/Paris`.

## Run tests

```sh
python3 -m unittest discover -s tests -v
```

## Notes

- The bot sends reminders only while it is running and can message the user; start a private chat with it first.
- Keep the bot token private. Do not commit `.env` or the SQLite database.
- This is a reminder and record-keeping tool, not accounting or financial advice. Keep independent copies of important records.
