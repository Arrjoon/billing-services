# Notes — Feature 1: Returns

## How returns work (without changing existing flow)

Invoice create, pay, and cancel are unchanged. Returns are a **new side path**:

1. Shop posts returned lines against an existing invoice.
2. We create a **SalesReturn** credit-note document (`RET-{number}`) plus line items.
3. Stock goes back up.
4. Invoice totals (`subtotal` / `tax` / `total`) are reduced so books and customer balance stay correct.
5. Original `InvoiceItem` rows are **not** edited or deleted (accountant still sees what was sold).

```
Existing flow:  create invoice → pay → cancel
New flow only:  POST /api/invoices/{id}/returns/
```

### Money rules (assumptions)

| Situation | What we do |
|---|---|
| Unpaid / partially paid, return some goods | Reduce invoice total → customer owes less |
| Already paid more than the new total | Cap `amount_paid` at new total; put the excess on `refund_amount` on the return document |
| Partial return (1 of 5) | Allowed; cannot return more than sold minus already returned |
| Cancelled / draft invoice | Rejected |

VAT on a return uses the same 13% rate as sales, applied to the returned subtotal.

## API

```http
POST /api/invoices/{id}/returns/
GET  /api/invoices/{id}/returns/
```

```json
{
  "reason": "Wrong size",
  "items": [
    {"invoice_item_id": 41, "quantity": 1}
  ]
}
```

## Assumptions

1. Refund vs store credit: record `refund_amount` on the return; shop settles cash/credit outside.
2. No time limit; accepted returns restock.
3. Never edit original invoice lines — keep a separate credit note.
4. Full return of unpaid invoice: totals go to zero; status becomes `paid`.

## Existing code changes

- None to `create_invoice`, `record_payment`, or `cancel_invoice`.
- Added `SalesReturn` / `SalesReturnItem`, `create_return`, serializers, `returns` action, admin, migration `0002_sales_returns`, and tests.
