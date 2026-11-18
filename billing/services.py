from decimal import Decimal

from django.db import transaction
from django.db.models import F, Max, Sum

from .exceptions import InsufficientStock, InvalidReturn
from .models import Invoice, InvoiceItem, Payment, Product, SalesReturn, SalesReturnItem
from .tasks import send_invoice_email

VAT_RATE = 0.13
VAT_RATE_DECIMAL = Decimal("0.13")


def next_invoice_number(business):
    last = Invoice.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def calculate_totals(invoice):
    subtotal = 0
    for item in invoice.items.all():
        subtotal += float(item.unit_price) * item.quantity
    tax = round(subtotal * VAT_RATE, 2)
    invoice.subtotal = subtotal
    invoice.tax = tax
    invoice.total = subtotal + tax


@transaction.atomic
def create_invoice(*, business, customer, due_date, items, user):
    invoice = Invoice.objects.create(
        business=business,
        customer=customer,
        number=next_invoice_number(business),
        status=Invoice.Status.ISSUED,
        due_date=due_date,
        created_by=user,
    )

    for row in items:
        product = Product.objects.get(pk=row["product_id"], business=business)
        quantity = row["quantity"]
        if product.stock < quantity:
            raise InsufficientStock(product.sku, product.stock, quantity)

        product.stock -= quantity
        product.save(update_fields=["stock"])

        InvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_price=product.unit_price,
            line_total=float(product.unit_price) * quantity,
        )

    calculate_totals(invoice)
    invoice.save(update_fields=["subtotal", "tax", "total"])

    send_invoice_email.delay(invoice.id)
    return invoice


@transaction.atomic
def record_payment(*, invoice, amount, method, user=None, gateway_ref=""):
    payment = Payment.objects.create(
        invoice=invoice,
        amount=amount,
        method=method,
        gateway_ref=gateway_ref,
        recorded_by=user,
    )

    invoice.amount_paid += amount
    if invoice.amount_paid >= invoice.total:
        invoice.status = Invoice.Status.PAID
    else:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    invoice.save(update_fields=["amount_paid", "status"])
    return payment


@transaction.atomic
def cancel_invoice(invoice):
    for item in invoice.items.select_related("product"):
        Product.objects.filter(pk=item.product_id).update(stock=F("stock") + item.quantity)
    invoice.status = Invoice.Status.CANCELLED
    invoice.save(update_fields=["status"])
    return invoice


def next_return_number(business):
    last = SalesReturn.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def _returned_quantity_for_item(invoice_item):
    total = invoice_item.return_items.aggregate(n=Sum("quantity"))["n"]
    return total or 0


def _recalculate_invoice_status(invoice):
    if invoice.amount_paid >= invoice.total:
        invoice.status = Invoice.Status.PAID
    elif invoice.amount_paid > 0:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    else:
        invoice.status = Invoice.Status.ISSUED


@transaction.atomic
def create_return(*, invoice, items, user=None, reason=""):
    """
    Record a sales return (credit note) without changing original invoice lines.

    - Restocks returned quantities
    - Reduces invoice subtotal/tax/total so books and customer balance stay correct
    - If amount_paid exceeds the new total, records refund_amount on the return
      and caps amount_paid at the new total
    """
    if invoice.status == Invoice.Status.CANCELLED:
        raise InvalidReturn("Cancelled invoices cannot be returned against.")
    if invoice.status == Invoice.Status.DRAFT:
        raise InvalidReturn("Draft invoices cannot be returned against.")
    if not items:
        raise InvalidReturn("At least one return item is required.")

    invoice_items = {
        item.id: item
        for item in invoice.items.select_related("product").all()
    }
    seen = set()
    prepared = []

    for row in items:
        invoice_item_id = row["invoice_item_id"]
        quantity = int(row["quantity"])
        if invoice_item_id in seen:
            raise InvalidReturn("Each invoice line can appear only once per return.")
        seen.add(invoice_item_id)

        if quantity < 1:
            raise InvalidReturn("Return quantity must be at least 1.")

        invoice_item = invoice_items.get(invoice_item_id)
        if invoice_item is None:
            raise InvalidReturn(f"Invoice item {invoice_item_id} does not belong to this invoice.")

        already_returned = _returned_quantity_for_item(invoice_item)
        remaining = invoice_item.quantity - already_returned
        if quantity > remaining:
            raise InvalidReturn(
                f"Cannot return {quantity} of '{invoice_item.product.name}'; "
                f"only {remaining} remaining."
            )

        unit_price = Decimal(str(invoice_item.unit_price))
        prepared.append(
            {
                "invoice_item": invoice_item,
                "product": invoice_item.product,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": unit_price * quantity,
            }
        )

    credit_subtotal = sum((row["line_total"] for row in prepared), Decimal("0.00"))
    credit_tax = (credit_subtotal * VAT_RATE_DECIMAL).quantize(Decimal("0.01"))
    credit_total = credit_subtotal + credit_tax

    sales_return = SalesReturn.objects.create(
        business=invoice.business,
        invoice=invoice,
        number=next_return_number(invoice.business),
        reason=reason or "",
        subtotal=credit_subtotal,
        tax=credit_tax,
        total=credit_total,
        created_by=user,
    )

    for row in prepared:
        SalesReturnItem.objects.create(
            sales_return=sales_return,
            invoice_item=row["invoice_item"],
            product=row["product"],
            quantity=row["quantity"],
            unit_price=row["unit_price"],
            line_total=row["line_total"],
        )
        Product.objects.filter(pk=row["product"].pk).update(stock=F("stock") + row["quantity"])

    invoice.subtotal = max(Decimal("0.00"), Decimal(str(invoice.subtotal)) - credit_subtotal)
    invoice.tax = max(Decimal("0.00"), Decimal(str(invoice.tax)) - credit_tax)
    invoice.total = max(Decimal("0.00"), Decimal(str(invoice.total)) - credit_total)

    refund_amount = Decimal("0.00")
    amount_paid = Decimal(str(invoice.amount_paid))
    if amount_paid > invoice.total:
        refund_amount = amount_paid - invoice.total
        invoice.amount_paid = invoice.total

    sales_return.refund_amount = refund_amount
    sales_return.save(update_fields=["refund_amount"])

    _recalculate_invoice_status(invoice)
    invoice.save(update_fields=["subtotal", "tax", "total", "amount_paid", "status"])
    return sales_return
