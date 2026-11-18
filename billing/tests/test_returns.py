from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from billing.exceptions import InvalidReturn
from billing.models import (
    Business,
    Customer,
    Invoice,
    InvoiceItem,
    Product,
    SalesReturn,
    StaffProfile,
)
from billing.services import create_return, record_payment

User = get_user_model()


class ReturnFeatureTests(TestCase):
    def setUp(self):
        self.business = Business.objects.create(name="Demo Shop", pan_number="PAN1")
        self.user = User.objects.create_user(username="owner", password="password123")
        StaffProfile.objects.create(
            user=self.user,
            business=self.business,
            role=StaffProfile.Role.OWNER,
        )
        self.customer = Customer.objects.create(
            business=self.business, name="Ram", phone="9800000001"
        )
        self.product_a = Product.objects.create(
            business=self.business,
            sku="A-1",
            name="Item A",
            unit_price=Decimal("100.00"),
            stock=10,
        )
        self.product_b = Product.objects.create(
            business=self.business,
            sku="B-1",
            name="Item B",
            unit_price=Decimal("50.00"),
            stock=10,
        )
        self.invoice = Invoice.objects.create(
            business=self.business,
            customer=self.customer,
            number=1,
            status=Invoice.Status.ISSUED,
            due_date=timezone.localdate(),
            created_by=self.user,
        )
        self.line_a = InvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product_a,
            quantity=3,
            unit_price=Decimal("100.00"),
            line_total=Decimal("300.00"),
        )
        self.line_b = InvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product_b,
            quantity=2,
            unit_price=Decimal("50.00"),
            line_total=Decimal("100.00"),
        )
        # Mirror create_invoice totals: (300+100) + 13% VAT
        self.invoice.subtotal = Decimal("400.00")
        self.invoice.tax = Decimal("52.00")
        self.invoice.total = Decimal("452.00")
        self.invoice.save(update_fields=["subtotal", "tax", "total"])
        self.product_a.stock = 7
        self.product_a.save(update_fields=["stock"])
        self.product_b.stock = 8
        self.product_b.save(update_fields=["stock"])

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_partial_return_unpaid_updates_stock_and_balance(self):
        sales_return = create_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.line_a.id, "quantity": 1}],
            user=self.user,
            reason="Damaged box",
        )

        self.invoice.refresh_from_db()
        self.product_a.refresh_from_db()

        self.assertEqual(sales_return.number, 1)
        self.assertEqual(sales_return.subtotal, Decimal("100.00"))
        self.assertEqual(sales_return.tax, Decimal("13.00"))
        self.assertEqual(sales_return.total, Decimal("113.00"))
        self.assertEqual(sales_return.refund_amount, Decimal("0.00"))
        self.assertEqual(self.product_a.stock, 8)
        self.assertEqual(self.invoice.subtotal, Decimal("300.00"))
        self.assertEqual(self.invoice.tax, Decimal("39.00"))
        self.assertEqual(self.invoice.total, Decimal("339.00"))
        self.assertEqual(self.invoice.status, Invoice.Status.ISSUED)
        self.assertEqual(self.invoice.balance, Decimal("339.00"))
        # Original invoice lines stay intact
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.quantity, 3)

    def test_full_return_paid_invoice_creates_refund(self):
        record_payment(
            invoice=self.invoice,
            amount=Decimal("452.00"),
            method="cash",
            user=self.user,
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

        sales_return = create_return(
            invoice=self.invoice,
            items=[
                {"invoice_item_id": self.line_a.id, "quantity": 3},
                {"invoice_item_id": self.line_b.id, "quantity": 2},
            ],
            user=self.user,
        )

        self.invoice.refresh_from_db()
        self.product_a.refresh_from_db()
        self.product_b.refresh_from_db()

        self.assertEqual(sales_return.total, Decimal("452.00"))
        self.assertEqual(sales_return.refund_amount, Decimal("452.00"))
        self.assertEqual(self.invoice.total, Decimal("0.00"))
        self.assertEqual(self.invoice.amount_paid, Decimal("0.00"))
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertEqual(self.product_a.stock, 10)
        self.assertEqual(self.product_b.stock, 10)

    def test_cannot_return_more_than_sold(self):
        create_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.line_b.id, "quantity": 1}],
            user=self.user,
        )
        with self.assertRaises(InvalidReturn):
            create_return(
                invoice=self.invoice,
                items=[{"invoice_item_id": self.line_b.id, "quantity": 2}],
                user=self.user,
            )

    def test_returns_api_creates_document(self):
        url = f"/api/invoices/{self.invoice.id}/returns/"
        response = self.client.post(
            url,
            {
                "reason": "Customer changed mind",
                "items": [{"invoice_item_id": self.line_a.id, "quantity": 1}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["return"]["document_number"], "RET-1")
        self.assertEqual(SalesReturn.objects.filter(invoice=self.invoice).count(), 1)

        list_response = self.client.get(url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)
