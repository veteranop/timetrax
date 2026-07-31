from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import IntegrityError, models, transaction
from django.db.models import Sum
from django.utils import timezone


class User(AbstractUser):
    """Custom user so we can grow roles + Google SSO without a painful migration."""

    class Role(models.TextChoices):
        OPERATOR = "operator", "Operator"       # Mark / staff
        CONTRACTOR = "contractor", "Contractor"  # added to specific work
        CLIENT = "client", "Client"             # submits/views their own tickets

    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.OPERATOR
    )

    def __str__(self):
        return self.get_full_name() or self.username


class Client(models.Model):
    """A billable relationship. Billing mode drives whether logged time is billable."""

    class BillingMode(models.TextChoices):
        CONTRACT = "contract", "Contract / retainer"  # e.g. Ascend — time informational
        HOURLY = "hourly", "Hourly"                    # e.g. Drop-in Radio — time billable

    name = models.CharField(max_length=200, unique=True)
    billing_mode = models.CharField(
        max_length=20, choices=BillingMode.choices, default=BillingMode.HOURLY
    )
    hourly_rate = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Only used when billing mode is Hourly.",
    )

    # Point of contact
    contact_name = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Stripe Customer id, cached on first invoice (like Cellfire caches it in Firebase).
    stripe_customer_id = models.CharField(max_length=40, blank=True, editable=False)

    # For clients on a Stripe subscription (e.g. Ascend's monthly retainer). Recorded
    # here so TimeTrax knows they're on a recurring plan; you set up the subscription in
    # Stripe and paste its id (sub_...). Subscription clients are exempt from hourly runs.
    stripe_subscription_id = models.CharField(
        max_length=40, blank=True,
        help_text="Stripe subscription id (sub_…) for clients on a recurring plan.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def stripe_subscription_url(self) -> str:
        """Deep-link to the client's subscription in the Stripe dashboard, if set."""
        if not self.stripe_subscription_id:
            return ""
        return f"{settings.STRIPE_DASHBOARD_BASE}/subscriptions/{self.stripe_subscription_id}"


class Item(models.Model):
    """Unified ticket = task. Stays open/closed only; can live a long time."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class Priority(models.TextChoices):
        HIGH = "H", "High"
        MEDIUM = "M", "Medium"
        LOW = "L", "Low"

    # Human-readable id: <Priority><YY><JJJ><NN>, e.g. H2700101
    # = High + year 27 + Julian day 001 + 1st High ticket that day.
    # Assigned once at creation and never changes.
    ticket_number = models.CharField(
        max_length=8, unique=True, null=True, blank=True, editable=False
    )
    priority = models.CharField(
        max_length=1, choices=Priority.choices, default=Priority.MEDIUM
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN
    )

    client = models.ForeignKey(
        Client, on_delete=models.PROTECT, related_name="items"
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="assigned_items",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="created_items",
    )

    # Obsidian-style backlinks: any item can relate to any other.
    related = models.ManyToManyField("self", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.ticket_number or f'#{self.pk}'} {self.title}"

    def _generate_ticket_number(self) -> str:
        """<Priority><YY><JJJ><NN>, counter resets per priority per local day."""
        now = timezone.localtime()
        prefix = f"{self.priority}{now.year % 100:02d}{now.timetuple().tm_yday:03d}"
        last = (
            Item.objects.filter(ticket_number__startswith=prefix)
            .order_by("-ticket_number")
            .first()
        )
        seq = (int(last.ticket_number[-2:]) + 1) if last else 1
        return f"{prefix}{seq:02d}"

    def save(self, *args, **kwargs):
        # Allocate the ticket number once, retrying on the rare same-day race.
        if not self.ticket_number:
            for _ in range(10):
                self.ticket_number = self._generate_ticket_number()
                try:
                    with transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError:
                    self.ticket_number = None
            raise IntegrityError("Could not allocate a unique ticket number")
        return super().save(*args, **kwargs)

    @property
    def total_hours(self) -> Decimal:
        """Sum of all work-note hours logged against this item."""
        return self.work_notes.aggregate(total=Sum("hours"))["total"] or Decimal("0")

    @property
    def is_billable(self) -> bool:
        return self.client.billing_mode == Client.BillingMode.HOURLY

    def close(self):
        self.status = self.Status.CLOSED
        self.closed_at = timezone.now()
        self.save(update_fields=["status", "closed_at", "updated_at"])

    def reopen(self):
        self.status = self.Status.OPEN
        self.closed_at = None
        self.save(update_fields=["status", "closed_at", "updated_at"])


class WorkNote(models.Model):
    """The billable logging unit: what was done + hours. Summed on close."""

    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name="work_notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    body = models.TextField()
    hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0"),
        help_text="Decimal hours: 0.5 = 30 min, 0.25 = 15 min, 1.75 = 1h45m.",
    )
    # Set once this note's hours land on a Stripe invoice — the guard against
    # double-billing. SET_NULL so voiding an invoice frees its hours to rebill.
    invoice = models.ForeignKey(
        "Invoice", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_notes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.hours}h on {self.item} by {self.author}"


class Invoice(models.Model):
    """A monthly billing run: an hourly client's unbilled hours gathered into one
    Stripe invoice (created as a DRAFT for operator review before sending)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Sent / open"
        PAID = "paid", "Paid"
        VOID = "void", "Void"
        UNCOLLECTIBLE = "uncollectible", "Uncollectible"

    client = models.ForeignKey(
        Client, on_delete=models.PROTECT, related_name="invoices"
    )
    stripe_invoice_id = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    period_start = models.DateField()
    period_end = models.DateField()
    total_hours = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0"),
        help_text="Invoice total in dollars (hours × client rate).",
    )
    hosted_invoice_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invoice {self.stripe_invoice_id} · {self.client} · ${self.amount}"

    @property
    def dashboard_url(self) -> str:
        """Deep-link to review/finalize this draft in the Stripe dashboard."""
        return f"{settings.STRIPE_DASHBOARD_BASE}/invoices/{self.stripe_invoice_id}"


class Comment(models.Model):
    """Discussion + event history. Hermes writes here; no hours attached."""

    class Source(models.TextChoices):
        HUMAN = "human", "Human"
        HERMES = "hermes", "Hermes"
        SYSTEM = "system", "System"

    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name="comments"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.HUMAN
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_source_display()} comment on {self.item}"
