import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import Database
from domain import (
    InputError,
    format_money,
    parse_clock,
    parse_date,
    parse_money,
    parse_offsets,
    reminder_offsets_to_send,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TITLE, AMOUNT, CURRENCY, DUE, FREQUENCY, CUSTOM, END_RULE, END_VALUE = range(8)
INITIAL_PAYMENT, PAYMENT_DATE, EDIT_FIELD, EDIT_VALUE, PAYMENT_AMOUNT, SETTINGS_TZ, SETTINGS_OFFSETS, SETTINGS_TIME = range(8, 16)


def load_local_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env()
db = Database()


def user_today(user_id: int) -> date:
    user = db.get_user(user_id)
    return datetime.now(ZoneInfo(user["timezone"])).date()


def skip_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip")]])


async def reject_group_callback(update: Update) -> bool:
    if update.effective_chat and update.effective_chat.type != "private":
        if update.callback_query:
            await update.callback_query.answer(
                "For privacy, use this bot in a private chat.", show_alert=True
            )
        return True
    return False


def text_state(callback, skippable=False):
    handlers = [CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE)]
    if skippable:
        handlers.append(CommandHandler("skip", skip_current, filters=filters.ChatType.PRIVATE))
    handlers.append(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            callback,
        )
    )
    if skippable:
        handlers.append(CallbackQueryHandler(skip_current, pattern="^skip$"))
    return handlers


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, keyboard=None):
    context.user_data["stage_prompt"] = text
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def send_next(update, context, text, state, keyboard=None):
    await ask(update, context, text, keyboard)
    return state


def frequency_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("One time", callback_data="freq:once"),
                InlineKeyboardButton("Weekly", callback_data="freq:weekly"),
            ],
            [
                InlineKeyboardButton("Monthly", callback_data="freq:monthly"),
                InlineKeyboardButton("Quarterly", callback_data="freq:quarterly"),
            ],
            [
                InlineKeyboardButton("Yearly", callback_data="freq:yearly"),
                InlineKeyboardButton("Custom", callback_data="freq:custom"),
            ],
            [InlineKeyboardButton("Skip (one time)", callback_data="freq:once")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "Hi! I can remind you about bills and keep a payment history.\n\n"
        "Use /add to create a reminder. At each step you can skip optional details.\n"
        "/list - view and manage invoices\n"
        "/upcoming - due soon and overdue\n"
        "/history - payment history\n"
        "/settings - timezone and reminder preferences\n"
        "/help - all commands and tips"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/add - add an invoice or recurring payment\n"
        "/list - list, edit, pause, or resume invoices\n"
        "/upcoming - see payments due in the next 30 days\n"
        "/history - review up to 50 recorded payments\n"
        "/settings - set timezone, reminder lead days, and reminder time\n"
        "/cancel - cancel the current setup\n\n"
        "You can skip optional answers. Use dates like 2026-12-15 and amounts "
        "like 49.99. Reminder days accept values such as 3,0 (three days before "
        "and on the due date). Mark paid records the remaining installment; "
        "Partial payment records a payment without advancing the schedule."
    )


async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.ensure_user(update.effective_user.id)
    context.user_data["draft"] = {}
    return await send_next(
        update,
        context,
        "What is this payment for? Send a name or skip for “Untitled invoice”.",
        TITLE,
        skip_keyboard(),
    )


async def get_title(update, context):
    value = update.effective_message.text.strip()
    context.user_data["draft"]["title"] = (
        "Untitled invoice" if value.lower() == "/skip" else value[:80] or "Untitled invoice"
    )
    return await send_next(update, context, "Amount? You can skip or enter a number like 49.99.", AMOUNT, skip_keyboard())


async def get_amount(update, context):
    text = update.effective_message.text.strip()
    draft = context.user_data["draft"]
    if text.lower() == "/skip":
        draft["amount_minor"] = None
    else:
        try:
            amount, currency = parse_money(text)
        except InputError as exc:
            await update.effective_message.reply_text(str(exc))
            return AMOUNT
        draft["amount_minor"] = amount
        draft["currency"] = currency
    if draft.get("currency"):
        return await send_next(update, context, "Next due date? Use YYYY-MM-DD, or skip if not known.", DUE, skip_keyboard())
    return await send_next(update, context, "Currency code? For example EUR or USD. Skip if you do not want to set one.", CURRENCY, skip_keyboard())


async def get_currency(update, context):
    text = update.effective_message.text.strip()
    if text.lower() == "/skip":
        context.user_data["draft"]["currency"] = None
    elif len(text) == 3 and text.isalpha():
        context.user_data["draft"]["currency"] = text.upper()
    else:
        await update.effective_message.reply_text("Enter a three-letter code such as EUR, or /skip.")
        return CURRENCY
    return await send_next(update, context, "Next due date? Use YYYY-MM-DD, or skip if not known.", DUE, skip_keyboard())


async def get_due(update, context):
    value = update.effective_message.text.strip()
    if value.lower() == "/skip":
        context.user_data["draft"]["next_due"] = None
    else:
        try:
            context.user_data["draft"]["next_due"] = parse_date(value).isoformat()
        except InputError as exc:
            await update.effective_message.reply_text(str(exc))
            return DUE
    return await send_next(update, context, "How often does it recur?", FREQUENCY, frequency_keyboard())


async def set_frequency(update, context):
    if await reject_group_callback(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    frequency = query.data.split(":", 1)[1]
    context.user_data["draft"]["frequency"] = frequency
    if frequency == "custom":
        return await send_next(update, context, "How many days between payments? Enter a number from 1 to 3650.", CUSTOM, skip_keyboard())
    context.user_data["draft"]["interval_days"] = None
    if frequency == "once":
        context.user_data["draft"]["end_after_count"] = 1
        context.user_data["draft"]["end_date"] = None
        return await send_next(update, context, "Have you already paid anything toward this invoice? Enter an amount or skip.", INITIAL_PAYMENT, skip_keyboard())
    return await send_next(update, context, "When should reminders stop?", END_RULE, end_keyboard())


def end_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("After X payments", callback_data="end:count"),
                InlineKeyboardButton("On a date", callback_data="end:date"),
            ],
            [
                InlineKeyboardButton("Keep going", callback_data="end:forever"),
                InlineKeyboardButton("Skip", callback_data="end:forever"),
            ],
        ]
    )


async def get_custom_interval(update, context):
    try:
        interval = int(update.effective_message.text.strip())
        if not 1 <= interval <= 3650:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a whole number from 1 to 3650.")
        return CUSTOM
    context.user_data["draft"]["interval_days"] = interval
    return await send_next(update, context, "When should reminders stop?", END_RULE, end_keyboard())


async def set_end_rule(update, context):
    if await reject_group_callback(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    rule = query.data.split(":", 1)[1]
    if rule == "count":
        context.user_data["draft"]["end_mode"] = "count"
        return await send_next(update, context, "After how many payments should I stop? Enter a positive whole number.", END_VALUE, skip_keyboard())
    if rule == "date":
        context.user_data["draft"]["end_mode"] = "date"
        return await send_next(update, context, "Enter the final date to remind you, in YYYY-MM-DD format.", END_VALUE, skip_keyboard())
    context.user_data["draft"]["end_after_count"] = None
    context.user_data["draft"]["end_date"] = None
    return await send_next(update, context, "Have you already paid anything toward this invoice? Enter an amount or skip.", INITIAL_PAYMENT, skip_keyboard())


async def get_end_value(update, context):
    value = update.effective_message.text.strip()
    if value.lower() == "/skip":
        context.user_data["draft"]["end_after_count"] = None
        context.user_data["draft"]["end_date"] = None
    elif context.user_data["draft"].get("end_mode") == "count":
        try:
            count = int(value)
            if count < 1:
                raise ValueError
            context.user_data["draft"]["end_after_count"] = count
            context.user_data["draft"]["end_date"] = None
        except ValueError:
            await update.effective_message.reply_text("Enter a positive whole number, or /skip.")
            return END_VALUE
    else:
        try:
            context.user_data["draft"]["end_date"] = parse_date(value).isoformat()
            context.user_data["draft"]["end_after_count"] = None
        except InputError as exc:
            await update.effective_message.reply_text(str(exc))
            return END_VALUE
    return await send_next(update, context, "Have you already paid anything toward this invoice? Enter an amount or skip.", INITIAL_PAYMENT, skip_keyboard())


async def get_initial_payment(update, context):
    value = update.effective_message.text.strip()
    if value.lower() == "/skip":
        return await save_draft(update, context)
    try:
        amount, currency = parse_money(value)
        if amount <= 0:
            raise InputError("Enter an amount greater than zero, or /skip.")
    except InputError as exc:
        await update.effective_message.reply_text(str(exc))
        return INITIAL_PAYMENT
    context.user_data["draft"]["initial_paid"] = amount
    if currency and not context.user_data["draft"].get("currency"):
        context.user_data["draft"]["currency"] = currency
    return await send_next(update, context, "When did you pay? Use YYYY-MM-DD, or skip for today.", PAYMENT_DATE, skip_keyboard())


async def get_payment_date(update, context):
    value = update.effective_message.text.strip()
    if value.lower() == "/skip":
        context.user_data["draft"]["initial_payment_date"] = user_today(update.effective_user.id).isoformat()
    else:
        try:
            context.user_data["draft"]["initial_payment_date"] = parse_date(value).isoformat()
        except InputError as exc:
            await update.effective_message.reply_text(str(exc))
            return PAYMENT_DATE
    return await save_draft(update, context)


async def save_draft(update, context):
    draft = context.user_data.pop("draft")
    user_id = update.effective_user.id
    settings = db.get_user(user_id)
    title = draft.get("title", "Untitled invoice")
    invoice_id = db.add_invoice(
        user_id,
        title,
        draft.get("amount_minor"),
        draft.get("currency"),
        draft.get("next_due"),
        draft.get("frequency", "once"),
        draft.get("interval_days"),
        draft.get("end_after_count", 1),
        draft.get("end_date"),
        json.loads(settings["reminder_offsets"]),
    )
    invoice = db.get_invoice(invoice_id, user_id)
    if draft.get("initial_paid"):
        db.record_payment(
            invoice,
            draft["initial_paid"],
            draft.get("initial_payment_date", user_today(user_id).isoformat()),
        )
    due = draft.get("next_due") or "not set"
    amount_text = format_money(draft.get("amount_minor"), draft.get("currency"))
    await update.effective_message.reply_text(
        "Saved “{}” (#{}).\nAmount: {}\nNext due: {}\nSchedule: {}\n"
        "Reminders use your settings. Use /list to manage it.".format(
            title,
            invoice_id,
            amount_text,
            due,
            draft.get("frequency", "once"),
        )
    )
    return ConversationHandler.END


async def skip_current(update, context):
    query = update.callback_query
    if await reject_group_callback(update):
        return ConversationHandler.END
    if query:
        await query.answer()
    stage = context.user_data.get("stage_prompt", "")
    if "name or skip" in stage:
        context.user_data["draft"]["title"] = "Untitled invoice"
        return await send_next(update, context, "Amount? You can skip or enter a number like 49.99.", AMOUNT, skip_keyboard())
    if stage.startswith("Amount?"):
        context.user_data["draft"]["amount_minor"] = None
        return await send_next(update, context, "Currency code? For example EUR or USD. Skip if you do not want to set one.", CURRENCY, skip_keyboard())
    if stage.startswith("Currency code"):
        context.user_data["draft"]["currency"] = None
        return await send_next(update, context, "Next due date? Use YYYY-MM-DD, or skip if not known.", DUE, skip_keyboard())
    if stage.startswith("Next due date"):
        context.user_data["draft"]["next_due"] = None
        return await send_next(update, context, "How often does it recur?", FREQUENCY, frequency_keyboard())
    if stage.startswith("Have you already paid"):
        return await save_draft(update, context)
    if stage.startswith("When did you pay"):
        context.user_data["draft"]["initial_payment_date"] = user_today(update.effective_user.id).isoformat()
        return await save_draft(update, context)
    if stage.startswith("How many days"):
        context.user_data["draft"]["frequency"] = "once"
        context.user_data["draft"]["interval_days"] = None
        context.user_data["draft"]["end_after_count"] = 1
        return await send_next(update, context, "Have you already paid anything toward this invoice? Enter an amount or skip.", INITIAL_PAYMENT, skip_keyboard())
    if stage.startswith("After how many payments") or stage.startswith("Enter the final date"):
        context.user_data["draft"]["end_after_count"] = None
        context.user_data["draft"]["end_date"] = None
        return await send_next(update, context, "Have you already paid anything toward this invoice? Enter an amount or skip.", INITIAL_PAYMENT, skip_keyboard())
    if stage.startswith("When should reminders stop"):
        context.user_data["draft"]["end_after_count"] = None
        context.user_data["draft"]["end_date"] = None
        return await send_next(update, context, "Have you already paid anything toward this invoice? Enter an amount or skip.", INITIAL_PAYMENT, skip_keyboard())
    if stage.startswith("Enter a date") and context.user_data.get("flow") == "payment":
        context.user_data["payment_date"] = user_today(update.effective_user.id).isoformat()
        return await save_partial_payment(update, context)
    if stage.startswith("When did you pay?"):
        context.user_data["payment_date"] = user_today(update.effective_user.id).isoformat()
        return await save_partial_payment(update, context)
    if stage.startswith("Enter payment date"):
        context.user_data["payment_date"] = user_today(update.effective_user.id).isoformat()
        return await save_partial_payment(update, context)
    if stage.startswith("Send the next due date"):
        db.update_invoice(
            context.user_data["edit_invoice_id"],
            update.effective_user.id,
            next_due=None,
        )
        await update.effective_message.reply_text("Due date cleared.")
        return ConversationHandler.END
    if stage.startswith("Enter a date") and context.user_data.get("flow") == "settings":
        return await finish_settings(update, context)
    if stage.startswith("Timezone"):
        return await ask_settings_offsets(update, context)
    if stage.startswith("Reminder lead days"):
        return await ask_settings_time(update, context)
    if stage.startswith("Reminder time"):
        return await finish_settings(update, context)
    await update.effective_message.reply_text("Nothing to skip at this step.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("draft", None)
    context.user_data.pop("flow", None)
    context.user_data.pop("edit_invoice_id", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def invoice_keyboard(invoice):
    due = invoice["next_due"] or "none"
    actions = []
    if invoice["status"] == "active":
        if invoice["next_due"]:
            actions.append(
                [
                    InlineKeyboardButton("Mark paid", callback_data="paid:{}:{}".format(invoice["id"], due)),
                    InlineKeyboardButton("Partial payment", callback_data="partial:{}:{}".format(invoice["id"], due)),
                ]
            )
            actions.append(
                [
                    InlineKeyboardButton("Snooze 1 day", callback_data="snooze:{}:{}".format(invoice["id"], due)),
                    InlineKeyboardButton("Edit", callback_data="edit:{}:{}".format(invoice["id"], due)),
                ]
            )
        else:
            actions.append([InlineKeyboardButton("Edit", callback_data="edit:{}:{}".format(invoice["id"], due))])
        actions.append([InlineKeyboardButton("Stop reminders", callback_data="stop:{}:{}".format(invoice["id"], due))])
    elif invoice["status"] == "paused":
        actions.extend(
            [
                [InlineKeyboardButton("Edit", callback_data="edit:{}:{}".format(invoice["id"], due))],
                [InlineKeyboardButton("Resume reminders", callback_data="resume:{}:{}".format(invoice["id"], due))],
            ]
        )
    else:
        actions.append([InlineKeyboardButton("Edit", callback_data="edit:{}:{}".format(invoice["id"], due))])
    return InlineKeyboardMarkup(actions)


def invoice_summary(invoice):
    due = invoice["next_due"] or "not set"
    frequency = invoice["frequency"]
    if frequency == "custom" and invoice["interval_days"]:
        frequency = "every {} days".format(invoice["interval_days"])
    summary = "• {} (#{}): {}, due {}, {}, {}".format(
        invoice["title"],
        invoice["id"],
        format_money(invoice["amount_minor"], invoice["currency"]),
        due,
        frequency,
        invoice["status"],
    )
    if invoice["end_date"]:
        summary += ", reminders through {}".format(invoice["end_date"])
    return summary


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    invoices = db.list_invoices(update.effective_user.id)
    if not invoices:
        await update.message.reply_text("No invoices yet. Use /add to create one.")
        return
    for invoice in invoices:
        outstanding = None
        if invoice["amount_minor"] is not None:
            outstanding = max(
                0,
                invoice["amount_minor"]
                - db.payment_total(invoice["id"], invoice["next_due"]),
            )
        text = invoice_summary(invoice)
        if outstanding is not None:
            text += "\nCurrent installment remaining: {}".format(
                format_money(outstanding, invoice["currency"])
            )
        await update.message.reply_text(text, reply_markup=invoice_keyboard(invoice))


async def upcoming_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    today = datetime.now(ZoneInfo(user["timezone"])).date()
    horizon = today + timedelta(days=30)
    found = []
    for invoice in db.list_invoices(update.effective_user.id, active_only=True):
        if invoice["next_due"]:
            due = date.fromisoformat(invoice["next_due"])
            within_end_date = (
                invoice["end_date"] is None
                or (
                    invoice["next_due"] <= invoice["end_date"]
                    and today.isoformat() <= invoice["end_date"]
                )
            )
            if due <= horizon and within_end_date:
                label = "OVERDUE" if due < today else "due"
                found.append("{} — {} ({})".format(invoice["next_due"], invoice["title"], label))
    await update.message.reply_text(
        "Upcoming and overdue:\n" + "\n".join(found)
        if found
        else "Nothing is due in the next 30 days."
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payments = db.list_payments(update.effective_user.id)
    if not payments:
        await update.message.reply_text("No payments recorded yet.")
        return
    lines = [
        "{} — {} — {} (due {})".format(
            row["payment_date"],
            row["title"],
            format_money(row["amount_minor"], row["currency"]),
            row["due_date"] or "unknown",
        )
        for row in payments
    ]
    await update.message.reply_text("Payment history:\n" + "\n".join(lines))


async def start_full_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_group_callback(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    _, raw_id, due = query.data.split(":", 2)
    invoice = db.get_invoice(int(raw_id), update.effective_user.id)
    if invoice is None or (invoice["next_due"] or "none") != due:
        await query.message.reply_text("That payment action is out of date. Use /list for current details.")
        return ConversationHandler.END
    if invoice["amount_minor"] is None:
        context.user_data["flow"] = "full_payment"
        context.user_data["payment_invoice_id"] = invoice["id"]
        await ask(update, context, "What amount did you pay? Enter the amount, or use /cancel.")
        return PAYMENT_AMOUNT
    result = db.complete_installment(invoice["id"], update.effective_user.id, user_today(update.effective_user.id).isoformat())
    if result == "unavailable":
        await query.message.reply_text("This invoice is not active. Resume it from /list if needed.")
    else:
        updated = db.get_invoice(invoice["id"], update.effective_user.id)
        await query.message.reply_text(
            "Payment recorded. " + ("This invoice is complete." if result == "completed" else "Next due: {}.".format(updated["next_due"] or "not set"))
        )
    return ConversationHandler.END


async def start_partial_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_group_callback(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    _, raw_id, due = query.data.split(":", 2)
    invoice = db.get_invoice(int(raw_id), update.effective_user.id)
    if invoice is None or (invoice["next_due"] or "none") != due:
        await query.message.reply_text("That payment action is out of date. Use /list for current details.")
        return ConversationHandler.END
    if invoice["status"] != "active":
        await query.message.reply_text("Resume this invoice before recording a payment.")
        return ConversationHandler.END
    context.user_data["flow"] = "payment"
    context.user_data["payment_invoice_id"] = invoice["id"]
    await ask(update, context, "Enter the partial payment amount (for example 25.00 EUR).")
    return PAYMENT_AMOUNT


async def get_partial_amount(update, context):
    try:
        amount, currency = parse_money(update.effective_message.text)
        if amount <= 0:
            raise InputError("Enter an amount greater than zero.")
    except InputError as exc:
        await update.effective_message.reply_text(str(exc))
        return PAYMENT_AMOUNT
    context.user_data["payment_amount"] = amount
    context.user_data["payment_currency"] = currency
    await ask(update, context, "Enter payment date (YYYY-MM-DD), or skip for today.", skip_keyboard())
    return PAYMENT_DATE


async def save_partial_payment(update, context):
    invoice = db.get_invoice(context.user_data["payment_invoice_id"], update.effective_user.id)
    if invoice is None:
        await update.effective_message.reply_text("Invoice not found.")
        return ConversationHandler.END
    amount = context.user_data["payment_amount"]
    currency = context.user_data.get("payment_currency")
    if currency and not invoice["currency"]:
        db.update_invoice(invoice["id"], update.effective_user.id, currency=currency)
        invoice = db.get_invoice(invoice["id"], update.effective_user.id)
    elif currency and currency != invoice["currency"]:
        await update.effective_message.reply_text(
            "This invoice uses {}. Enter the payment again in that currency."
            .format(invoice["currency"])
        )
        context.user_data.pop("payment_amount", None)
        context.user_data.pop("payment_currency", None)
        await ask(update, context, "Enter the payment amount again.")
        return PAYMENT_AMOUNT
    payment_date = context.user_data.get("payment_date", user_today(update.effective_user.id).isoformat())
    db.record_payment(invoice, amount, payment_date)
    if context.user_data.get("flow") == "full_payment":
        result = db.complete_installment(
            invoice["id"],
            update.effective_user.id,
            payment_date,
        )
        context.user_data.pop("flow", None)
        updated = db.get_invoice(invoice["id"], update.effective_user.id)
        await update.effective_message.reply_text(
            "Payment recorded. "
            + (
                "This invoice is complete."
                if result == "completed"
                else "Next due: {}.".format(updated["next_due"] or "not set")
            )
        )
        return ConversationHandler.END
    context.user_data.pop("flow", None)
    await update.effective_message.reply_text(
        "Partial payment saved: {}. Use /list to see the remaining installment balance.".format(
            format_money(amount, invoice["currency"])
        )
    )
    return ConversationHandler.END


async def invoice_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_group_callback(update):
        return
    query = update.callback_query
    await query.answer()
    action, raw_id, due = query.data.split(":", 2)
    invoice = db.get_invoice(int(raw_id), update.effective_user.id)
    if invoice is None:
        await query.message.reply_text("Invoice not found.")
        return
    if (invoice["next_due"] or "none") != due:
        await query.message.reply_text("That action is out of date. Use /list for current details.")
        return
    if action == "snooze":
        if invoice["status"] != "active" or not invoice["next_due"]:
            await query.message.reply_text("This invoice has no active due date to snooze.")
            return
        user = db.get_user(update.effective_user.id)
        today = datetime.now(ZoneInfo(user["timezone"])).date()
        remind_on = today + timedelta(days=1)
        db.add_snooze(invoice, remind_on.isoformat())
        await query.message.reply_text("Snoozed until {}.".format(remind_on.isoformat()))
    elif action == "stop":
        db.set_status(invoice["id"], update.effective_user.id, "paused")
        await query.message.reply_text("Reminders paused. Use /list and Resume reminders to restart.")
    elif action == "resume":
        db.set_status(invoice["id"], update.effective_user.id, "active")
        await query.message.reply_text("Reminders resumed.")


async def start_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_group_callback(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    _, raw_id, due = query.data.split(":", 2)
    invoice = db.get_invoice(int(raw_id), update.effective_user.id)
    if invoice is None or (invoice["next_due"] or "none") != due:
        await query.message.reply_text("That edit action is out of date. Use /list for current details.")
        return ConversationHandler.END
    context.user_data["edit_invoice_id"] = invoice["id"]
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Name", callback_data="field:title"), InlineKeyboardButton("Amount / currency", callback_data="field:amount")],
            [InlineKeyboardButton("Next due date", callback_data="field:due"), InlineKeyboardButton("Repeat schedule", callback_data="field:frequency")],
            [InlineKeyboardButton("Stop date", callback_data="field:end"), InlineKeyboardButton("Reminder days", callback_data="field:reminders")],
            [InlineKeyboardButton("Cancel", callback_data="editcancel")],
        ]
    )
    await ask(update, context, "What would you like to edit?", keyboard)
    return EDIT_FIELD


async def edit_field(update, context):
    if await reject_group_callback(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    if query.data == "editcancel":
        await query.message.reply_text("Edit cancelled.")
        return ConversationHandler.END
    field = query.data.split(":", 1)[1]
    context.user_data["edit_field"] = field
    prompts = {
        "title": "Send the new name.",
        "amount": "Send the amount and optional currency, e.g. 49.99 EUR. Send 0 to clear the amount.",
        "due": "Send the next due date in YYYY-MM-DD, or /skip to clear it.",
        "frequency": "Send once, weekly, monthly, quarterly, yearly, or custom:<days>.",
        "end": "Send a final date (YYYY-MM-DD), count:<number>, or forever.",
        "reminders": "Send reminder lead days like 3,0 (days before due, including 0 for due date).",
    }
    await ask(update, context, prompts[field], skip_keyboard() if field == "due" else None)
    return EDIT_VALUE


async def edit_value(update, context):
    invoice_id = context.user_data["edit_invoice_id"]
    user_id = update.effective_user.id
    field = context.user_data["edit_field"]
    value = update.effective_message.text.strip()
    try:
        if field == "title":
            db.update_invoice(invoice_id, user_id, title=value[:80] or "Untitled invoice")
        elif field == "amount":
            amount, currency = parse_money(value)
            existing = db.get_invoice(invoice_id, user_id)
            db.update_invoice(
                invoice_id,
                user_id,
                amount_minor=None if amount == 0 else amount,
                currency=currency or existing["currency"],
            )
        elif field == "due":
            db.update_invoice(invoice_id, user_id, next_due=None if value.lower() == "/skip" else parse_date(value).isoformat())
        elif field == "frequency":
            if value.startswith("custom:"):
                interval = int(value.split(":", 1)[1])
                if not 1 <= interval <= 3650:
                    raise InputError("Custom interval must be from 1 to 3650 days.")
                db.update_invoice(
                    invoice_id,
                    user_id,
                    frequency="custom",
                    interval_days=interval,
                    end_after_count=None,
                    end_date=None,
                )
            elif value in {"once", "weekly", "monthly", "quarterly", "yearly"}:
                db.update_invoice(
                    invoice_id,
                    user_id,
                    frequency=value,
                    interval_days=None,
                    end_after_count=1 if value == "once" else None,
                    end_date=None,
                )
            else:
                raise InputError("Choose once, weekly, monthly, quarterly, yearly, or custom:<days>.")
        elif field == "end":
            if value == "forever":
                db.update_invoice(invoice_id, user_id, end_date=None, end_after_count=None)
            elif value.startswith("count:"):
                count = int(value.split(":", 1)[1])
                if count < 1:
                    raise InputError("Count must be positive.")
                db.update_invoice(invoice_id, user_id, end_after_count=count, end_date=None)
            else:
                db.update_invoice(invoice_id, user_id, end_date=parse_date(value).isoformat(), end_after_count=None)
        elif field == "reminders":
            db.update_invoice(invoice_id, user_id, reminder_offsets=parse_offsets(value))
    except (InputError, ValueError) as exc:
        await update.effective_message.reply_text(str(exc))
        return EDIT_VALUE
    context.user_data.pop("edit_invoice_id", None)
    await update.effective_message.reply_text("Updated. Use /list to view the changes.")
    return ConversationHandler.END


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["flow"] = "settings"
    user = db.get_user(update.effective_user.id)
    await ask(update, context, "Timezone? Current: {}. Enter an IANA zone such as Europe/Paris, or skip.".format(user["timezone"]), skip_keyboard())
    return SETTINGS_TZ


async def get_settings_timezone(update, context):
    value = update.effective_message.text.strip()
    if value.lower() == "/skip":
        return await ask_settings_offsets(update, context)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        await update.effective_message.reply_text("Unknown timezone. Use an IANA name like Europe/Paris, or /skip.")
        return SETTINGS_TZ
    db.update_user(update.effective_user.id, timezone=value)
    return await ask_settings_offsets(update, context)


async def ask_settings_offsets(update, context):
    user = db.get_user(update.effective_user.id)
    context.user_data["stage_prompt"] = "Reminder days"
    return await send_next(update, context, "Reminder lead days? Current: {}. Enter values like 3,0, or skip.".format(user["reminder_offsets"]), SETTINGS_OFFSETS, skip_keyboard())


async def get_settings_offsets(update, context):
    value = update.effective_message.text.strip()
    if value.lower() != "/skip":
        try:
            db.update_user(update.effective_user.id, reminder_offsets=json.dumps(parse_offsets(value)))
        except InputError as exc:
            await update.effective_message.reply_text(str(exc))
            return SETTINGS_OFFSETS
    return await ask_settings_time(update, context)


async def ask_settings_time(update, context):
    user = db.get_user(update.effective_user.id)
    return await send_next(update, context, "Reminder time in your timezone? Current: {}. Enter HH:MM or skip.".format(user["reminder_time"]), SETTINGS_TIME, skip_keyboard())


async def get_settings_time(update, context):
    value = update.effective_message.text.strip()
    if value.lower() != "/skip":
        try:
            db.update_user(update.effective_user.id, reminder_time=parse_clock(value).strftime("%H:%M"))
        except InputError as exc:
            await update.effective_message.reply_text(str(exc))
            return SETTINGS_TIME
    return await finish_settings(update, context)


async def finish_settings(update, context):
    context.user_data.pop("flow", None)
    await update.effective_message.reply_text("Reminder settings saved.")
    return ConversationHandler.END


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(timezone.utc)
    for invoice in db.list_all_active_invoices():
        try:
            user = db.get_user(invoice["user_id"])
            local_now = now_utc.astimezone(ZoneInfo(user["timezone"]))
            if local_now.strftime("%H:%M") < user["reminder_time"] or not invoice["next_due"]:
                continue
            due = date.fromisoformat(invoice["next_due"])
            today = local_now.date()
            if invoice["end_date"] and today > date.fromisoformat(invoice["end_date"]):
                continue
            offsets = json.loads(invoice["reminder_offsets"])
            for offset in reminder_offsets_to_send(due, today, offsets):
                if not db.has_notification(invoice["id"], invoice["next_due"], offset):
                    await context.bot.send_message(
                        chat_id=invoice["user_id"],
                        text="Reminder: {} is due {} ({}).".format(
                            invoice["title"],
                            invoice["next_due"],
                            format_money(invoice["amount_minor"], invoice["currency"]),
                        ),
                        reply_markup=invoice_keyboard(invoice),
                    )
                    db.mark_notification_sent(invoice["id"], invoice["next_due"], offset)
            for snooze in db.due_snoozes(today.isoformat()):
                if snooze["invoice_id"] == invoice["id"]:
                    await context.bot.send_message(
                        chat_id=invoice["user_id"],
                        text="Snoozed reminder: {} is due {}.".format(snooze["title"], snooze["due_date"]),
                        reply_markup=invoice_keyboard(invoice),
                    )
                    db.mark_snooze_sent(snooze["id"])
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.exception("Could not process reminders for invoice %s", invoice["id"])
        except Exception:
            logger.exception("Failed to send reminder for invoice %s", invoice["id"])


async def post_init(application: Application):
    application.job_queue.run_repeating(reminder_job, interval=60, first=5, name="invoice-reminders")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error is not None:
        logger.error(
            "Unhandled bot error",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Unhandled bot error without exception details")


def build_application(token: str) -> Application:
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add", start_add, filters=filters.ChatType.PRIVATE),
            CommandHandler("settings", settings_command, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(start_edit, pattern=r"^edit:\d+:[^:]+$"),
            CallbackQueryHandler(start_partial_payment, pattern=r"^partial:\d+:[^:]+$"),
            CallbackQueryHandler(start_full_payment, pattern=r"^paid:\d+:[^:]+$"),
        ],
        states={
            TITLE: text_state(get_title, skippable=True),
            AMOUNT: text_state(get_amount, skippable=True),
            CURRENCY: text_state(get_currency, skippable=True),
            DUE: text_state(get_due, skippable=True),
            FREQUENCY: [CallbackQueryHandler(set_frequency, pattern="^freq:"), CallbackQueryHandler(skip_current, pattern="^skip$")],
            CUSTOM: text_state(get_custom_interval, skippable=True),
            END_RULE: [CallbackQueryHandler(set_end_rule, pattern="^end:"), CallbackQueryHandler(skip_current, pattern="^skip$")],
            END_VALUE: text_state(get_end_value, skippable=True),
            INITIAL_PAYMENT: text_state(get_initial_payment, skippable=True),
            PAYMENT_DATE: text_state(get_payment_date, skippable=True),
            EDIT_FIELD: [CallbackQueryHandler(edit_field, pattern="^field:"), CallbackQueryHandler(edit_field, pattern="^editcancel$")],
            EDIT_VALUE: [
                CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE),
                CommandHandler("skip", skip_current, filters=filters.ChatType.PRIVATE),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    edit_value,
                ),
                CallbackQueryHandler(skip_current, pattern="^skip$"),
            ],
            PAYMENT_AMOUNT: text_state(get_partial_amount),
            SETTINGS_TZ: text_state(get_settings_timezone, skippable=True),
            SETTINGS_OFFSETS: text_state(get_settings_offsets, skippable=True),
            SETTINGS_TIME: text_state(get_settings_time, skippable=True),
        },
        fallbacks=[
            CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(skip_current, pattern="^skip$"),
        ],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("help", help_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("list", list_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("upcoming", upcoming_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("history", history_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(invoice_action, pattern=r"^(snooze|stop|resume):\d+:[^:]+$"))
    application.add_error_handler(error_handler)
    return application


def main():
    load_local_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your environment or .env file.")
    application = build_application(token)
    application.run_polling()


if __name__ == "__main__":
    main()
