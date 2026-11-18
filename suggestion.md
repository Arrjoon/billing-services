# Suggestions — existing code flow bugs and how to fix them

Bugs found in the **original** invoice / payment / report flow (before or beside the new features).  
Each item: what is wrong → why it matters → how to solve.

---

## Critical

### 1. Customer `phone` should be compulsory

**Where:** `Customer.phone` is `CharField(..., blank=True)`; `CustomerSerializer` does not require it.

**Risk:** Overdue payment reminders (Feature 2) and gateway “payment received” SMS both need a phone. Customers created without one are silently skipped (`no_phone`), so shops think reminders are “broken” while unpaid invoices pile up. Outstanding collections by SMS cannot work reliably.

**Fix:**
- Model: `phone = models.CharField(max_length=20)` — remove `blank=True` (keep a sensible max length)
- Serializer: require non-empty phone on create/update (`allow_blank=False`, optional format validator for Nepal mobiles e.g. `98xxxxxxxx`)
- Migration: backfill or block migrate until existing blank phones are filled
- API: `POST /api/customers/` without `phone` → `400`
- Tests: create customer without phone fails; reminder path still skips only legacy blank rows if any remain during transition

---

### 2. Payments ignore business scope (tenant leak)

**Where:** `InvoiceViewSet.payments`

```python
invoice = Invoice.objects.get(pk=pk)  # not scoped
```

Cancel correctly uses `self.get_object()`; payments do not.

**Risk:** Staff from shop A can pay shop B’s invoice if they know the ID.

**Fix:** Use `invoice = self.get_object()`. Add a test: business A cannot pay business B’s invoice.

---

### 3. SQL injection in outstanding report

**Where:** `OutstandingCustomersView`

```python
AND LOWER(c.name) LIKE LOWER('%%{search}%%')
```

**Risk:** `q` is pasted into raw SQL.

**Fix:** Bind the search value, e.g. `LIKE %s` with `[f"%{search}%"]`, or use the ORM / `Q` objects. Add a test with a malicious `q`.

---

### 4. Stock can go negative under concurrency

**Where:** `create_invoice` — read stock, then subtract, then save (no row lock).

**Risk:** Two invoices for the last unit can both pass the check → oversell.

**Fix:** Inside `@transaction.atomic`, load products with `select_for_update()`, or:

```sql
UPDATE ... SET stock = stock - %s WHERE id = %s AND stock >= %s
```

and fail if 0 rows updated. Optionally add `CheckConstraint(stock >= 0)`.

---

### 5. Payments can race / overpay / pay bad statuses

**Where:** `record_payment`, `PaymentCreateSerializer`, gateway task.

**Risks:**
- Concurrent payments → `amount_paid` > `total`
- Amount can be ≤ 0
- No check against remaining balance
- Cancelled (or already paid) invoices can still be paid

**Fix:**
- `Invoice.objects.select_for_update().get(...)`
- Reject `cancelled` / already `paid` (define overpay policy separately)
- Validate `amount > 0` and `amount <= balance`
- Same rules on the gateway path

---

### 6. Gateway webhook not idempotent

**Where:** `process_gateway_payment` + `Payment.gateway_ref`

**Risk:** Duplicate webhook delivery creates two payments.

**Fix:** Partial unique constraint on non-blank `gateway_ref`. If ref already exists → no-op success. Test duplicate payloads.

---

### 7. PATCH can rewrite money and tenant fields

**Where:** `InvoiceSerializer` (`fields = "__all__"`) + PATCH allowed.

**Risk:** Client can change `business`, `total`, `amount_paid`, `status`, `number`, etc.

**Fix:** Make money/status/identity fields read-only. Only allow safe edits (e.g. `due_date`) or disable PATCH until policy is clear.

---

### 8. Invoice numbers can collide

**Where:** `next_invoice_number` = `Max(number) + 1` with no unique `(business, number)`.

**Risk:** Two concurrent creates can share the same number.

**Fix:** `UniqueConstraint(fields=["business", "number"])` plus `select_for_update` on a counter (or retry on integrity error).

---

## High

### 9. Money math uses float

**Where:** `calculate_totals`, `line_total=float(...)`.

**Risk:** Rounding drift vs `DecimalField` storage.

**Fix:** Use `Decimal` everywhere (`VAT_RATE = Decimal("0.13")`), quantize to 2 places.

---

### 10. Cross-business links possible in DB

**Risk:** Invoice for shop A can point at a customer/product from shop B via admin/ORM.

**Fix:** Validate `customer.business_id == invoice.business_id` (and products) in the service / `clean()`. Longer term: DB constraints.

---

### 11. Cancel / payment edge cases

**Risks:**
- Cancel checks status, not `payments.exists()`
- Unscoped payment / gateway can still hit cancelled invoices
- Create path does not lock stock the way cancel restocks with `F()`

**Fix:** Reject pay-on-cancelled; cancel only with no payments (or explicit policy); use locked atomic stock updates on both paths.

---

### 12. Sales report date range

**Where:** `created_at__range=(start, end)` with date objects.

**Risk:** End day can be truncated; accountants often want `issue_date`. `vat_collected` is VAT on totals, not cash VAT.

**Fix:** Filter `issue_date__gte` / `__lte`; rename fields to match meaning.

---

## Medium

### 13. Roles exist but are unused

`StaffProfile.Role` never gates actions — any staff can sell, pay, cancel, report.

**Fix:** DRF permissions by role (cashier sell/pay; accountant reports; owner all).

---

### 14. Missing `StaffProfile` → 500

`current_business` assumes `request.user.profile` exists.

**Fix:** Permission/middleware → 403 “No staff profile”.

---

### 15. Secrets / DEBUG in settings

Hardcoded `SECRET_KEY`, gateway `API_KEY`, `DEBUG = True`.

**Fix:** Load from env; production-safe defaults.

---

### 16. Celery task before commit

`send_invoice_email.delay(invoice.id)` runs inside the transaction.

**Risk:** Worker can load the invoice before commit (or after rollback).

**Fix:** `transaction.on_commit(lambda: send_invoice_email.delay(invoice.id))`.

---

## Suggested fix order

1. Make customer `phone` compulsory (model + serializer + migration/backfill)  
2. Scope payments with `get_object()` + block pay on cancelled  
3. Parameterize outstanding SQL  
4. Lock stock and payments; reject ≤0 / overpay  
5. Gateway `gateway_ref` uniqueness / idempotency  
6. Lock down Invoice PATCH fields  
7. Unique invoice numbers  
8. Decimal money math  
9. `on_commit` for Celery  
10. Role permissions + missing-profile 403  
11. Tests for every fix you ship  

Feature work (returns / reminders) is documented in `NOTES.md`. Prefer fixing items 1–3 before real customer traffic.
