from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from billing.models import Business, Customer, Invoice, PaymentReminder, StaffProfile
from billing.reminders import is_quiet_hours, run_payment_reminders

User = get_user_model()
KTM = ZoneInfo("Asia/Kathmandu")


@override_settings(
    REMINDER_QUIET_HOURS_START=21,
    REMINDER_QUIET_HOURS_END=8,
    REMINDER_COOLDOWN_HOURS=24,
    REMINDER_MAX_PER_CUSTOMER_PER_DAY=1,
)
class PaymentReminderTests(TestCase):
    def setUp(self):
        self.business = Business.objects.create(name="Demo Shop")
        self.user = User.objects.create_user(username="owner", password="password123")
        StaffProfile.objects.create(
            user=self.user, business=self.business, role=StaffProfile.Role.OWNER
        )
        self.customer = Customer.objects.create(
            business=self.business,
            name="Ram",
            phone="9800000001",
        )
        self.customer_b = Customer.objects.create(
            business=self.business,
            name="Sita",
            phone="9800000002",
        )
        today = timezone.localdate()
        self.overdue = self._make_invoice(
            self.customer, number=1, due_date=today - timedelta(days=5), total="113.00"
        )
        self.overdue_same_phone = self._make_invoice(
            self.customer, number=2, due_date=today - timedelta(days=3), total="226.00"
        )
        self.current = self._make_invoice(
            self.customer_b, number=3, due_date=today + timedelta(days=3), total="50.00"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _make_invoice(self, customer, *, number, due_date, total):
        return Invoice.objects.create(
            business=self.business,
            customer=customer,
            number=number,
            status=Invoice.Status.ISSUED,
            due_date=due_date,
            subtotal=Decimal(total) / Decimal("1.13"),
            tax=Decimal("0"),
            total=Decimal(total),
            amount_paid=Decimal("0"),
            created_by=self.user,
        )

    def test_quiet_hours_detection(self):
        night = datetime(2026, 11, 18, 22, 0, tzinfo=KTM)
        morning = datetime(2026, 11, 18, 7, 0, tzinfo=KTM)
        day = datetime(2026, 11, 18, 10, 0, tzinfo=KTM)
        self.assertTrue(is_quiet_hours(night))
        self.assertTrue(is_quiet_hours(morning))
        self.assertFalse(is_quiet_hours(day))

    @patch("billing.reminders.send_sms")
    def test_sends_during_day_and_skips_second_invoice_same_phone(self, mock_sms):
        now = datetime(2026, 11, 18, 10, 30, tzinfo=KTM)
        summary = run_payment_reminders(now=now, business=self.business)

        self.assertFalse(summary.quiet_hours)
        self.assertEqual(summary.sent, 1)
        self.assertEqual(summary.skipped, 1)  # second invoice, same phone, daily limit
        self.assertEqual(mock_sms.call_count, 1)
        self.assertEqual(PaymentReminder.objects.filter(status="sent").count(), 1)

        actions = {r.invoice_id: r.action for r in summary.results}
        self.assertEqual(actions[self.overdue.id], "sent")
        self.assertEqual(actions[self.overdue_same_phone.id], "skipped")

    @patch("billing.reminders.send_sms")
    def test_no_send_during_quiet_hours(self, mock_sms):
        now = datetime(2026, 11, 18, 22, 15, tzinfo=KTM)
        summary = run_payment_reminders(now=now, business=self.business)

        self.assertTrue(summary.quiet_hours)
        self.assertEqual(summary.sent, 0)
        self.assertEqual(summary.considered, 0)
        mock_sms.assert_not_called()
        self.assertEqual(PaymentReminder.objects.count(), 0)

    @patch("billing.reminders.send_sms")
    def test_cooldown_skips_recently_reminded_invoice(self, mock_sms):
        now = datetime(2026, 11, 18, 11, 0, tzinfo=KTM)
        PaymentReminder.objects.create(
            business=self.business,
            invoice=self.overdue,
            customer=self.customer,
            phone=self.customer.phone,
            message="prior",
            status=PaymentReminder.Status.SENT,
            sent_at=now - timedelta(hours=2),
        )
        # Force sent_at (auto_now_add); update explicitly
        PaymentReminder.objects.filter(invoice=self.overdue).update(
            sent_at=now - timedelta(hours=2)
        )

        summary = run_payment_reminders(now=now, business=self.business)
        # overdue skipped by cooldown; overdue_same_phone can send (different invoice, same phone
        # but first was skipped so phone not "messaged today" from this run's successful send —
        # wait: prior reminder today means phone is in phones_today, so same phone limited.
        # overdue: cooldown; overdue_same_phone: customer_daily_limit
        self.assertEqual(summary.sent, 0)
        self.assertEqual(summary.skipped, 2)
        mock_sms.assert_not_called()

    @patch("billing.reminders.send_sms")
    def test_dry_run_does_not_send(self, mock_sms):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=KTM)
        summary = run_payment_reminders(dry_run=True, now=now, business=self.business)

        self.assertEqual(summary.sent, 1)
        self.assertTrue(any(r.action == "would_send" for r in summary.results))
        mock_sms.assert_not_called()
        self.assertEqual(PaymentReminder.objects.count(), 0)

    @patch("billing.reminders.send_sms")
    def test_skips_paid_and_not_overdue(self, mock_sms):
        self.overdue.status = Invoice.Status.PAID
        self.overdue.amount_paid = self.overdue.total
        self.overdue.save(update_fields=["status", "amount_paid"])
        self.overdue_same_phone.status = Invoice.Status.PAID
        self.overdue_same_phone.amount_paid = self.overdue_same_phone.total
        self.overdue_same_phone.save(update_fields=["status", "amount_paid"])

        now = datetime(2026, 11, 18, 10, 0, tzinfo=KTM)
        summary = run_payment_reminders(now=now, business=self.business)

        self.assertEqual(summary.considered, 0)
        mock_sms.assert_not_called()

    @patch("billing.reminders.send_sms")
    def test_api_run_and_list_reminders(self, mock_sms):
        # Freeze "now" inside the view by patching run to use daytime
        daytime = datetime(2026, 11, 18, 10, 0, tzinfo=KTM)

        with patch("billing.views.run_payment_reminders") as mock_run:
            from billing.reminders import run_payment_reminders as real_run

            mock_run.side_effect = lambda **kwargs: real_run(
                dry_run=kwargs.get("dry_run", False),
                now=daytime,
                business=kwargs.get("business"),
            )
            response = self.client.post(
                "/api/reminders/run/", {"dry_run": False}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sent"], 1)
        self.assertEqual(response.data["business_id"], self.business.id)
        mock_sms.assert_called_once()

        list_response = self.client.get(f"/api/invoices/{self.overdue.id}/reminders/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(list_response.data[0]["status"], "sent")

    def test_settings_endpoint(self):
        response = self.client.get("/api/reminders/settings/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["quiet_hours_start"], 21)
        self.assertEqual(response.data["quiet_hours_end"], 8)
        self.assertEqual(response.data["cooldown_hours"], 24)
        self.assertEqual(response.data["max_per_customer_per_day"], 1)

    @patch("billing.reminders.send_sms", side_effect=RuntimeError("SMS down"))
    def test_failed_send_is_logged(self, mock_sms):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=KTM)
        # Only one overdue customer phone path: mark second invoice paid so one attempt fails
        self.overdue_same_phone.status = Invoice.Status.PAID
        self.overdue_same_phone.amount_paid = self.overdue_same_phone.total
        self.overdue_same_phone.save(update_fields=["status", "amount_paid"])

        summary = run_payment_reminders(now=now, business=self.business)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(PaymentReminder.objects.filter(status="failed").count(), 1)
