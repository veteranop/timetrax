"""Stripe billing for TimeTrax.

Turns logged, billable hours into Stripe invoices. Design (decided 2026-07-14):

- **Monthly billing run:** gather an hourly client's unbilled hours for a period
  into ONE invoice.
- **Draft, operator reviews:** invoices are created as DRAFTS (``auto_advance=False``).
  Mark reviews in the Stripe dashboard and sends — nothing reaches a client unseen.
- **Retainers billed separately:** contract/retainer clients (e.g. Ascend) are a fixed
  recurring charge; their hours stay informational and are NOT invoiced per-hour here.

All calls are OUTBOUND HTTPS to Stripe — no webhooks/public URL needed, so this runs
fine on a local-only install. (Hearing back "invoice paid" would need webhooks later.)
"""

from collections import defaultdict
from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from .models import Client, Invoice, WorkNote


class BillingError(Exception):
    """A user-facing billing problem (show the message; don't 500)."""


def _stripe():
    key = settings.STRIPE_SECRET_KEY
    if not key:
        raise BillingError(
            "No Stripe key configured. Add STRIPE_SECRET_KEY to your .env "
            "(copy .env.example), then restart the server."
        )
    stripe.api_key = key
    return stripe


def unbilled_worknotes(client, period_start, period_end):
    """Not-yet-invoiced worknotes for a client, logged within [start, end]."""
    return (
        WorkNote.objects
        .filter(
            item__client=client,
            invoice__isnull=True,
            created_at__date__gte=period_start,
            created_at__date__lte=period_end,
        )
        .select_related("item")
        .order_by("item__ticket_number", "created_at")
    )


def logged_hours(client, period_start, period_end):
    """Total hours logged for a client in a period, regardless of billing status.
    Used to show subscription/retainer clients' tracked (informational) time."""
    return (
        WorkNote.objects
        .filter(
            item__client=client,
            created_at__date__gte=period_start,
            created_at__date__lte=period_end,
        )
        .aggregate(t=Sum("hours"))["t"] or Decimal("0")
    )


def preview_client(client, period_start, period_end):
    """Dashboard summary for one client — hours + amount, no Stripe calls."""
    notes = unbilled_worknotes(client, period_start, period_end)
    hours = notes.aggregate(t=Sum("hours"))["t"] or Decimal("0")
    rate = client.hourly_rate or Decimal("0")
    return {
        "client": client,
        "hours": hours,
        "rate": rate,
        "amount": (hours * rate).quantize(Decimal("0.01")),
        "note_count": notes.count(),
        "has_rate": bool(client.hourly_rate),
    }


def _as_data(listing):
    """Stripe list results are dict-like OR objects — normalise to a plain list."""
    return listing.get("data") if isinstance(listing, dict) else listing.data


def sync_client_to_stripe(client):
    """Bond a TimeTrax Client to a Stripe Customer and push its name/email.

    TimeTrax is the source of truth → Stripe mirrors it. Dedups carefully so a
    'marriage' never creates a duplicate customer: it reuses the customer behind
    a linked subscription, or an existing customer matched by email, before
    creating a new one. Returns an action string for reporting.
    """
    s = _stripe()

    # 1) Already linked → push current name/email to Stripe.
    if client.stripe_customer_id:
        s.Customer.modify(
            client.stripe_customer_id,
            name=client.name,
            email=client.contact_email or None,
        )
        return "updated"

    # 2) On a subscription but not linked → adopt THAT subscription's customer
    #    (creating a new one would orphan the subscription — e.g. Ascend).
    if client.stripe_subscription_id:
        sub = s.Subscription.retrieve(client.stripe_subscription_id)
        customer_id = sub.get("customer") if isinstance(sub, dict) else sub.customer
        if customer_id:
            client.stripe_customer_id = customer_id
            client.save(update_fields=["stripe_customer_id"])
            s.Customer.modify(customer_id, name=client.name, email=client.contact_email or None)
            return "linked via subscription"

    # 3) Match an existing customer by email before creating a fresh one.
    if client.contact_email:
        found = _as_data(s.Customer.list(email=client.contact_email, limit=1))
        if found:
            cust = found[0]
            customer_id = cust.id if hasattr(cust, "id") else cust["id"]
            client.stripe_customer_id = customer_id
            client.save(update_fields=["stripe_customer_id"])
            s.Customer.modify(customer_id, name=client.name)
            return "linked by email"

    # 4) No match → create a new customer.
    customer = s.Customer.create(
        name=client.name,
        email=client.contact_email or None,
        metadata={"timetrax_client_id": client.id},
    )
    client.stripe_customer_id = customer.id
    client.save(update_fields=["stripe_customer_id"])
    return "created"


def sync_all_clients():
    """Reconcile every client with Stripe (the manual re-sync). Returns
    ({action: count}, [errors])."""
    from collections import Counter

    _stripe()  # fail fast with a clean message if no key
    counts, errors = Counter(), []
    for c in Client.objects.all():
        try:
            counts[sync_client_to_stripe(c)] += 1
        except Exception as e:  # one bad client shouldn't abort the whole run
            errors.append(f"{c.name}: {e}")
    return dict(counts), errors


def get_or_create_customer(client):
    """Return the client's Stripe Customer id, bonding it (with dedup) on first use."""
    if not client.stripe_customer_id:
        sync_client_to_stripe(client)
    return client.stripe_customer_id


def create_draft_invoice(client, period_start, period_end, user=None):
    """Gather one client's unbilled hours in a period into a DRAFT Stripe invoice.

    Returns the local Invoice. Raises BillingError for anything the operator
    should just read and fix (no rate, no hours, wrong billing mode).
    """
    if client.billing_mode != Client.BillingMode.HOURLY:
        raise BillingError(
            f"{client} is a {client.get_billing_mode_display()} client — "
            "retainers are billed as a fixed recurring charge, not per-hour."
        )
    if not client.hourly_rate:
        raise BillingError(f"{client} has no hourly rate set (add one in the admin).")

    notes = list(unbilled_worknotes(client, period_start, period_end))
    if not notes:
        raise BillingError(f"No unbilled hours for {client} in that period.")

    s = _stripe()
    customer_id = get_or_create_customer(client)
    rate = client.hourly_rate

    # One invoice line per ticket = tidy for the client.
    by_item = defaultdict(list)
    for n in notes:
        by_item[n.item].append(n)

    # Create the draft first, then attach line items to it explicitly.
    invoice = s.Invoice.create(
        customer=customer_id,
        collection_method="send_invoice",
        days_until_due=settings.INVOICE_DAYS_UNTIL_DUE,
        auto_advance=False,  # stay a DRAFT — operator reviews & sends
        pending_invoice_items_behavior="exclude",
        metadata={
            "timetrax_client_id": client.id,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        },
    )

    total_hours = Decimal("0")
    for item, item_notes in by_item.items():
        item_hours = sum((n.hours for n in item_notes), Decimal("0"))
        total_hours += item_hours
        amount_cents = int((item_hours * rate * 100).quantize(Decimal("1")))
        description = (
            f"{item.ticket_number} — {item.title} "
            f"({item_hours}h @ ${rate}/hr)"
        )[:250]
        s.InvoiceItem.create(
            customer=customer_id,
            invoice=invoice.id,
            amount=amount_cents,
            currency="usd",
            description=description,
            metadata={"timetrax_item_id": item.id},
        )

    amount = (total_hours * rate).quantize(Decimal("0.01"))

    # Persist locally + stamp the worknotes invoiced, atomically. This is the
    # guard against ever billing the same hours twice.
    with transaction.atomic():
        local = Invoice.objects.create(
            client=client,
            stripe_invoice_id=invoice.id,
            status=Invoice.Status.DRAFT,
            period_start=period_start,
            period_end=period_end,
            total_hours=total_hours,
            amount=amount,
            hosted_invoice_url=invoice.get("hosted_invoice_url") or "",
            created_by=user,
        )
        WorkNote.objects.filter(pk__in=[n.pk for n in notes]).update(invoice=local)

    return local
