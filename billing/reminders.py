"""
Overdue payment SMS reminders.

Rules (see NOTES.md):
- Only issued / partially_paid invoices past due_date, with a customer phone
- No SMS during quiet hours (default 21:00–08:00 local time)
- At most one successful reminder per invoice within the cooldown window
- At most N successful reminders per customer phone per local calendar day
  (stops spam when one customer has many overdue invoices)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import Invoice, PaymentReminder
from .tasks import send_sms

logger = logging.getLogger(__name__)


@dataclass
class ReminderResult:
    invoice_id: int
    invoice_number: int
    customer_id: int
    phone: str
    action: str  # sent | would_send | skipped | failed
    reason: str = ""
    reminder_id: int | None = None


@dataclass
class ReminderRunSummary:
    dry_run: bool
    quiet_hours: bool
    now: str
    considered: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[ReminderResult] = field(default_factory=list)

    def as_dict(self):
        return {
            "dry_run": self.dry_run,
            "quiet_hours": self.quiet_hours,
            "now": self.now,
            "considered": self.considered,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "results": [
                {
                    "invoice_id": r.invoice_id,
                    "invoice_number": r.invoice_number,
                    "customer_id": r.customer_id,
                    "phone": r.phone,
                    "action": r.action,
                    "reason": r.reason,
                    "reminder_id": r.reminder_id,
                }
                for r in self.results
            ],
        }


def reminder_settings():
    return {
        "quiet_hours_start": getattr(settings, "REMINDER_QUIET_HOURS_START", 21),
        "quiet_hours_end": getattr(settings, "REMINDER_QUIET_HOURS_END", 8),
        "cooldown_hours": getattr(settings, "REMINDER_COOLDOWN_HOURS", 24),
        "max_per_customer_per_day": getattr(settings, "REMINDER_MAX_PER_CUSTOMER_PER_DAY", 1),
        "timezone": str(timezone.get_current_timezone()),
    }


def is_quiet_hours(now: datetime | None = None) -> bool:
    """True during [start, 24) U [0, end) in local time. Default 21:00–08:00."""
    now = timezone.localtime(now or timezone.now())
    start = int(getattr(settings, "REMINDER_QUIET_HOURS_START", 21))
    end = int(getattr(settings, "REMINDER_QUIET_HOURS_END", 8))
    hour = now.hour
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def build_reminder_message(invoice: Invoice) -> str:
    balance = invoice.balance
    return (
        f"Reminder from {invoice.business.name}: invoice #{invoice.number} "
        f"is overdue. Balance due: Rs. {balance}. Due was {invoice.due_date:%d %b %Y}."
    )


def overdue_invoices_qs(as_of_date=None, business=None):
    as_of_date = as_of_date or timezone.localdate()
    qs = (
        Invoice.objects.filter(
            due_date__lt=as_of_date,
            status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID],
        )
        .exclude(Q(customer__phone="") | Q(customer__phone__isnull=True))
        .filter(total__gt=0)
        .select_related("customer", "business")
        .order_by("due_date", "id")
    )
    if business is not None:
        qs = qs.filter(business=business)
    return qs


def _phones_messaged_today(now: datetime, business=None) -> set[str]:
    local = timezone.localtime(now)
    day_start = timezone.make_aware(datetime.combine(local.date(), time.min), local.tzinfo)
    day_end = day_start + timedelta(days=1)
    qs = PaymentReminder.objects.filter(
        status=PaymentReminder.Status.SENT,
        sent_at__gte=day_start,
        sent_at__lt=day_end,
    )
    if business is not None:
        qs = qs.filter(business=business)
    return set(qs.values_list("phone", flat=True))


def _last_sent_at_by_invoice(invoice_ids: list[int]) -> dict[int, datetime]:
    """Portable latest-sent map (works on SQLite and Postgres)."""
    if not invoice_ids:
        return {}
    latest: dict[int, datetime] = {}
    for invoice_id, sent_at in (
        PaymentReminder.objects.filter(
            invoice_id__in=invoice_ids,
            status=PaymentReminder.Status.SENT,
        )
        .order_by("-sent_at")
        .values_list("invoice_id", "sent_at")
    ):
        if invoice_id not in latest:
            latest[invoice_id] = sent_at
    return latest


def run_payment_reminders(
    *, dry_run: bool = False, now: datetime | None = None, business=None
) -> ReminderRunSummary:
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    summary = ReminderRunSummary(
        dry_run=dry_run,
        quiet_hours=is_quiet_hours(now),
        now=local_now.isoformat(),
    )

    if summary.quiet_hours:
        # Deliberate no-op: never SMS at night. Beat can still wake hourly.
        return summary

    cooldown = timedelta(hours=int(getattr(settings, "REMINDER_COOLDOWN_HOURS", 24)))
    max_per_day = int(getattr(settings, "REMINDER_MAX_PER_CUSTOMER_PER_DAY", 1))

    invoices = list(overdue_invoices_qs(as_of_date=local_now.date(), business=business))
    summary.considered = len(invoices)
    last_sent = _last_sent_at_by_invoice([inv.id for inv in invoices])
    phones_today = _phones_messaged_today(now, business=business)
    # Track phones we send to in this run so one batch can't spam the same number
    phones_this_run: dict[str, int] = {}

    for invoice in invoices:
        phone = (invoice.customer.phone or "").strip()
        if not phone:
            summary.skipped += 1
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone="",
                    action="skipped",
                    reason="no_phone",
                )
            )
            continue

        if invoice.balance <= 0:
            summary.skipped += 1
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone=phone,
                    action="skipped",
                    reason="zero_balance",
                )
            )
            continue

        previous = last_sent.get(invoice.id)
        if previous and (now - previous) < cooldown:
            summary.skipped += 1
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone=phone,
                    action="skipped",
                    reason="cooldown",
                )
            )
            continue

        count_today = (1 if phone in phones_today else 0) + phones_this_run.get(phone, 0)
        if count_today >= max_per_day:
            summary.skipped += 1
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone=phone,
                    action="skipped",
                    reason="customer_daily_limit",
                )
            )
            continue

        message = build_reminder_message(invoice)

        if dry_run:
            summary.sent += 1
            phones_this_run[phone] = phones_this_run.get(phone, 0) + 1
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone=phone,
                    action="would_send",
                    reason=message,
                )
            )
            continue

        try:
            send_sms(phone, message)
            reminder = PaymentReminder.objects.create(
                business=invoice.business,
                invoice=invoice,
                customer=invoice.customer,
                phone=phone,
                message=message,
                status=PaymentReminder.Status.SENT,
            )
            summary.sent += 1
            phones_this_run[phone] = phones_this_run.get(phone, 0) + 1
            phones_today.add(phone)
            last_sent[invoice.id] = now
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone=phone,
                    action="sent",
                    reminder_id=reminder.id,
                )
            )
        except Exception as exc:
            logger.exception("Failed to send reminder for invoice %s", invoice.id)
            reminder = PaymentReminder.objects.create(
                business=invoice.business,
                invoice=invoice,
                customer=invoice.customer,
                phone=phone,
                message=message,
                status=PaymentReminder.Status.FAILED,
                error=str(exc)[:500],
            )
            summary.failed += 1
            summary.results.append(
                ReminderResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.number,
                    customer_id=invoice.customer_id,
                    phone=phone,
                    action="failed",
                    reason=str(exc)[:200],
                    reminder_id=reminder.id,
                )
            )

    return summary
