# Billing service

A small Django REST Framework service for shops: customers, products with stock,
invoices with 13% VAT, payments (cash, bank, online gateway), sales returns,
overdue SMS payment reminders, and two reports.
Each staff user belongs to one business, and every business sees only its own data.

## Setup

You need Docker with Compose. Everything else runs in containers: the API on
Python 3.13, PostgreSQL, Redis, a Celery worker, and Celery Beat (reminder schedule).

```bash
cp .env.example .env
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

Every variable in `.env` is passed to the `web`, `worker` and `db` containers.
Restart the containers after changing it.

The API is at http://localhost:8000/api/ and the admin at http://localhost:8000/admin/.
Create a `Business` and a `StaffProfile` for your user in the admin.

Migrations are applied each time the `web` container starts. Your working copy is
mounted into the containers, so the API reloads when you edit code. The Celery
worker and beat do not auto-reload, so restart them after changing a task:

```bash
docker compose restart worker beat
```

Other commands you will need:

```bash
docker compose exec web python manage.py makemigrations
docker compose exec -e CELERY_TASK_ALWAYS_EAGER=1 web python manage.py test
docker compose logs -f worker     # task output, including emails
docker compose down -v            # stop and delete the database
```

`CELERY_TASK_ALWAYS_EAGER=1` runs Celery tasks inline instead of sending them to
the worker. Rebuild with `docker compose up --build` after changing
`requirements.txt`.

## API

| Method | Path | What it does |
|---|---|---|
| GET, POST | `/api/customers/` | List or add customers |
| GET, POST | `/api/products/` | List or add products |
| GET | `/api/invoices/?status=issued` | List invoices |
| POST | `/api/invoices/` | Create an invoice and reduce stock |
| GET, PATCH | `/api/invoices/{id}/` | View or edit an invoice |
| POST | `/api/invoices/{id}/payments/` | Record a payment |
| POST | `/api/invoices/{id}/cancel/` | Cancel an invoice and restock |
| GET, POST | `/api/invoices/{id}/returns/` | List or create a sales return (credit note) |
| GET | `/api/invoices/{id}/reminders/` | Payment reminder history for an invoice |
| GET | `/api/reminders/settings/` | Quiet hours / cooldown / daily SMS cap |
| POST | `/api/reminders/run/` | Run overdue SMS reminders (`{"dry_run": true}`) |
| GET | `/api/reports/sales/?from=2026-11-01&to=2026-11-30` | Sales summary |
| GET | `/api/reports/outstanding/?q=ram` | Customers with unpaid balances |
| POST | `/api/webhooks/paygate/` | Payment gateway webhook |

Reminders: Celery Beat runs hourly; SMS is skipped 21:00–08:00 local time, capped at
one SMS per customer phone per day and one per invoice per 24h. See [NOTES.md](NOTES.md).

Example invoice request:

```json
{
  "customer_id": 12,
  "due_date": "2026-12-15",
  "items": [
    {"product_id": 3, "quantity": 2},
    {"product_id": 7, "quantity": 1}
  ]
}
```

## Your task

See [BRIEF.md](BRIEF.md).
