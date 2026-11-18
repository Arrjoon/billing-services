# NOTES

Questions we would have asked, assumptions we made, how we built each feature, alternatives we rejected, and what we would do next.

---

## Feature 1: Returns

### What the business asked for

Customers return all items or only some. Some invoices are paid, some are not. After a return, stock, books, and customer balance must be correct, and the accountant needs a document.

### How we built it

We **did not change** `create_invoice` / `record_payment` / `cancel_invoice`.

Returns are a **new side path**:

1. `POST /api/invoices/{id}/returns/` with `{ reason?, items: [{ invoice_item_id, quantity }] }`
2. Create `SalesReturn` + `SalesReturnItem` credit note (`RET-{n}`) — the accountant document
3. Restock returned quantities
4. Reduce invoice `subtotal` / `tax` / `total` (13% VAT on returned subtotal)
5. If `amount_paid` > new total → set `refund_amount` on the return and cap `amount_paid`
6. Original `InvoiceItem` rows stay unchanged (sale history intact)

```
Old flow (unchanged):  create invoice → pay → cancel
New flow:              POST /api/invoices/{id}/returns/
List:                  GET  /api/invoices/{id}/returns/
```

### Assumptions

| Question | Decision |
|---|---|
| Refund vs store credit? | Record `refund_amount` on the return; shop settles cash/credit outside the API |
| Edit original invoice lines? | Never — separate credit note |
| Damaged goods restock? | Always restock; shop can refuse before calling the API |
| Time limit on returns? | None for now |
| Over-return? | Rejected (sold qty − already returned) |

### Alternatives (and why not)

| Alternative | Why not |
|---|---|
| **A. Edit / shrink original invoice lines** | Loses sale history; accountant cannot see “sold 5, returned 1” |
| **B. Only use Cancel** | Full invoice only; blocked when paid; not a return document |
| **C. Negative invoice with no line link** | Harder to restock correctly and stop over-returning |
| **D. Auto-create a negative Payment for refunds** | Stronger ledger, but mixes “money in” with refunds; we kept refund on the return doc for this task |
| **E. Customer store-credit wallet** | Valid product choice; extra scope the brief did not require |

### Why this way

- Partial + full returns work
- Paid and unpaid both land correctly
- Stock and balance stay consistent in one transaction
- Accountant gets `RET-{n}`
- Existing sell/pay/cancel API stays stable

---

## Feature 2: Payment reminders

### What the business asked for

SMS customers when invoices are overdue. Do **not** send several messages a day or messages at night.

### How we built it

**Celery Beat (hourly) + shared policy engine + audit log.**

```
Celery Beat (hourly)
  → billing.tasks.send_overdue_payment_reminders
      → billing.reminders.run_payment_reminders()
          → overdue invoices (issued / partially_paid, due_date < today, has phone)
          → skip quiet hours
          → skip invoice cooldown
          → skip if phone already hit daily SMS cap
          → send_sms() + write PaymentReminder row
```

Manual / shop-scoped API (same engine):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/reminders/settings/` | Quiet hours / cooldown / daily cap |
| `POST` | `/api/reminders/run/` | Run now (`{"dry_run": true\|false}`) |
| `GET` | `/api/invoices/{id}/reminders/` | History for one invoice |

Docker: `beat` service runs `celery -A config beat`.

### Anti-spam rules (assumptions)

| Rule | Default | Why |
|---|---|---|
| Quiet hours | 21:00–08:00 `Asia/Kathmandu` | No night SMS |
| Per-invoice cooldown | 24 hours | Same invoice not nagged repeatedly |
| Per-phone daily cap | 1 SMS / phone / local day | 5 overdue invoices → one SMS, not five |
| Eligible statuses | `issued`, `partially_paid` | Paid / cancelled / draft ignored |
| Phone required | skip if blank | Cannot SMS |

Env knobs: `REMINDER_QUIET_HOURS_START` / `END`, `REMINDER_COOLDOWN_HOURS`, `REMINDER_MAX_PER_CUSTOMER_PER_DAY`.

Failed sends are stored as `PaymentReminder(status=failed)` so one bad number does not stop the batch.

### Alternatives (and why not)

| Alternative | Why not |
|---|---|
| **A. One Celery ETA per invoice at due+1** | Hard to change quiet hours globally; must revoke tasks on pay/cancel; daily cap still needs shared state |
| **B. OS/K8s cron + `manage.py`** | Extra ops outside Compose; we already have Celery/Redis |
| **C. Twilio / Customer.io campaigns** | Mature, but vendor lock-in and brief wants code in this service |
| **D. Digest SMS (“3 overdue, Rs X”)** | Good follow-up; we chose per-invoice message + phone daily cap first |
| **E. WhatsApp / email instead of SMS** | Channel decision; brief asked for SMS |
| **F. django-celery-beat DB schedules** | Runtime edits without redeploy; extra dependency — static `CELERY_BEAT_SCHEDULE` is enough here |

### Why this way

- Reuses Celery, Redis, and existing `send_sms`
- One policy module shared by Beat and the manual API
- `PaymentReminder` is the audit trail
- `dry_run` previews without spending SMS credit
- Quiet hours + cooldown + daily phone cap match the complaint in the brief

---

## Existing code we changed

| Area | Change |
|---|---|
| Returns | `SalesReturn` / `SalesReturnItem`, `create_return`, returns API, admin, `0002_sales_returns`, tests |
| Reminders | `PaymentReminder`, `reminders.py`, Beat task, settings, `beat` service, reminder APIs, `0003_payment_reminders`, tests |
| `create_invoice` / `record_payment` / `cancel_invoice` | **Unchanged** on purpose |

Known pre-existing bugs in the old flow (tenant payment scope, raw SQL search, float money, stock races, etc.) are listed with fixes in **`suggestion.md`** — not fixed in this pass so feature diffs stay clear.

---

## What we would do with another day

- Fix critical items in `suggestion.md` (payment tenant scope, SQL injection, locks, Decimal, webhook idempotency)
- Customer `sms_opt_out` + per-business quiet hours
- Digest SMS option
- Optional refund payment / ledger row when `refund_amount > 0`
- Role-based permissions (cashier vs owner for `reminders/run`)
- Scale overdue reminder runs (see section below)

---

## How night stop works + how we solve very large overdue volume

### Night stop (already implemented)

Celery Beat still wakes every hour (including at night). The stop is an **early return** inside `run_payment_reminders`:

1. `is_quiet_hours(now)` checks local time (`Asia/Kathmandu`).
2. Default window: **21:00–08:00** → `hour >= 21` or `hour < 8`.
3. If quiet → return empty `ReminderRunSummary` immediately.
4. No overdue query, no SMS, no `PaymentReminder` rows.

So “no messages at night” is enforced in the policy engine, not by turning Beat off.

### Large overdue volume — current behaviour

Today one run does:

```python
invoices = list(overdue_invoices_qs(...))  # all matching rows in memory
for invoice in invoices:
    send_sms(...)
```

Fine for a few hundred shops with modest overdue lists. Weak if there are **tens of thousands** of overdue invoices: high memory, long task, SMS provider rate limits, and Beat’s next hour can overlap a slow run.

Anti-spam (daily phone cap + 24h cooldown) limits **how many SMS we send**, but we can still **scan** a huge set.

### How we can solve large overdue volume (next steps)

| Approach | How it helps |
|---|---|
| **1. Chunk / batch the run** | Process e.g. 200–500 invoices per task (`id__gt=last_id` / limit). When the chunk finishes, enqueue the next chunk. Caps memory and task length. |
| **2. One task per business** | Beat lists businesses and calls `send_overdue_for_business.delay(business_id)`. Spreads work across workers; one huge shop does not block all others. |
| **3. DB index on overdue query** | Add index e.g. `(status, due_date)` or `(business_id, status, due_date)` so “find overdue” stays fast as the invoice table grows. |
| **4. Avoid loading everything** | Use queryset `.iterator()` or keyset pagination instead of `list(qs)` for huge sets. |
| **5. Cap SMS per run / hour** | e.g. max 200 sends per hourly tick; remainder wait for the next hour. Protects the SMS gateway and task timeout. |
| **6. Keep oldest-due first** | Already `order_by("due_date", "id")` — keep this so limited capacity goes to the most overdue first. |
| **7. Push cooldown into SQL** | Exclude invoices that already have a `sent` reminder in the last 24h in the query, so we do not pull them into Python only to skip. |
| **8. Fan-out send tasks (careful)** | `send_one_reminder.delay(invoice_id)` for parallelism — but daily phone-cap must stay consistent (lock / shared counter), or we risk double SMS. |

Recommended production shape:

```
Beat (hourly)
  → if quiet hours: stop
  → for each business (or one global chunk starter):
        remind_chunk(after_id=0, limit=500)
          → process batch
          → if more rows: remind_chunk(after_id=last_id, limit=500)
```

That keeps night behaviour as-is, and makes “very large overdue” manageable without rewriting the anti-spam rules.
