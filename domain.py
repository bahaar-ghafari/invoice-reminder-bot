from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Tuple


class InputError(ValueError):
    pass


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise InputError("Use a date in YYYY-MM-DD format.") from exc


def parse_money(value: str) -> Tuple[int, Optional[str]]:
    parts = value.strip().split()
    if not parts:
        raise InputError("Enter an amount, such as 49.99.")
    if len(parts) > 2:
        raise InputError("Enter an amount and optional currency, such as 49.99 EUR.")
    try:
        amount = Decimal(parts[0])
    except InvalidOperation as exc:
        raise InputError("Enter a valid amount, such as 49.99.") from exc
    if not amount.is_finite() or amount < 0:
        raise InputError("The amount must be zero or more.")
    currency = parts[1].upper() if len(parts) == 2 else None
    if currency is not None and (len(currency) != 3 or not currency.isalpha()):
        raise InputError("Currency must be a three-letter code, such as EUR.")
    minor = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return minor, currency


def format_money(minor: Optional[int], currency: Optional[str]) -> str:
    if minor is None:
        return "not provided"
    amount = Decimal(minor) / 100
    return "{} {}".format(amount.quantize(Decimal("0.01")), currency or "")


def parse_offsets(value: str):
    try:
        offsets = sorted(set(int(part.strip()) for part in value.split(",")))
    except (ValueError, AttributeError) as exc:
        raise InputError("Enter reminder days as comma-separated numbers, like 3,0.") from exc
    if not offsets or any(item < 0 or item > 365 for item in offsets):
        raise InputError("Reminder days must be between 0 and 365.")
    return offsets


def add_months(value: date, months: int, anchor_day: Optional[int] = None) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = anchor_day or value.day
    return date(year, month, min(day, monthrange(year, month)[1]))


def next_occurrence(
    due: date,
    frequency: str,
    interval_days: Optional[int] = None,
    anchor_day: Optional[int] = None,
) -> Optional[date]:
    if frequency == "weekly":
        return due + timedelta(days=7)
    if frequency == "monthly":
        return add_months(due, 1, anchor_day)
    if frequency == "quarterly":
        return add_months(due, 3, anchor_day)
    if frequency == "yearly":
        return add_months(due, 12, anchor_day)
    if frequency == "custom" and interval_days:
        return due + timedelta(days=interval_days)
    return None


def reminder_offsets_to_send(due: date, today: date, offsets):
    return [
        offset
        for offset in offsets
        if (
            offset == 0 and today >= due
        )
        or (
            offset > 0
            and due - timedelta(days=offset) <= today < due
        )
    ]


def parse_clock(value: str):
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise InputError("Use 24-hour time in HH:MM format, for example 09:00.") from exc
