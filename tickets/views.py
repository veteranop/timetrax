import calendar
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import billing
from .forms import CommentForm, TicketForm, WorkNoteForm
from .models import Client, Comment, Invoice, Item, WorkNote


@login_required
def ticket_list(request):
    status = request.GET.get("status", "open")   # open | closed | all
    client_id = request.GET.get("client", "")

    tickets = Item.objects.select_related("client", "assignee")
    if status == "open":
        tickets = tickets.filter(status=Item.Status.OPEN)
    elif status == "closed":
        tickets = tickets.filter(status=Item.Status.CLOSED)
    if client_id:
        tickets = tickets.filter(client_id=client_id)

    return render(request, "tickets/ticket_list.html", {
        "tickets": tickets,
        "clients": Client.objects.all(),
        "status": status,
        "client_id": client_id,
        "open_count": Item.objects.filter(status=Item.Status.OPEN).count(),
    })


@login_required
def ticket_detail(request, pk):
    item = get_object_or_404(Item.objects.select_related("client", "assignee"), pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_note":
            form = WorkNoteForm(request.POST)
            if form.is_valid():
                note = form.save(commit=False)
                note.item = item
                note.author = request.user
                note.save()
                item.save(update_fields=["updated_at"])
        elif action == "add_comment":
            form = CommentForm(request.POST)
            if form.is_valid():
                comment = form.save(commit=False)
                comment.item = item
                comment.author = request.user
                comment.source = Comment.Source.HUMAN
                comment.save()
                item.save(update_fields=["updated_at"])
        elif action == "close":
            item.close()
        elif action == "reopen":
            item.reopen()
        return redirect("tickets:detail", pk=item.pk)

    # Merge work notes + comments into one chronological history.
    history = [{"kind": "work", "obj": w, "when": w.created_at} for w in item.work_notes.all()]
    history += [{"kind": "comment", "obj": c, "when": c.created_at} for c in item.comments.all()]
    history.sort(key=lambda e: e["when"])

    return render(request, "tickets/ticket_detail.html", {
        "item": item,
        "history": history,
        "work_form": WorkNoteForm(),
        "comment_form": CommentForm(),
    })


@login_required
def ticket_create(request):
    if request.method == "POST":
        form = TicketForm(request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.created_by = request.user
            item.save()
            return redirect("tickets:detail", pk=item.pk)
    else:
        form = TicketForm()
    return render(request, "tickets/ticket_form.html", {"form": form})


@login_required
def worknote_edit(request, pk):
    note = get_object_or_404(WorkNote, pk=pk)
    if request.method == "POST":
        if "delete" in request.POST:
            item_pk = note.item.pk
            note.delete()
            return redirect("tickets:detail", pk=item_pk)
        form = WorkNoteForm(request.POST, instance=note)
        if form.is_valid():
            form.save()
            return redirect("tickets:detail", pk=note.item.pk)
    else:
        form = WorkNoteForm(instance=note)
    return render(request, "tickets/entry_edit.html", {
        "form": form, "entry": note, "kind": "Work note",
    })


def _period(year, month):
    """First and last calendar day of a given month."""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


@login_required
def billing_dashboard(request):
    """Monthly billing run: pick a month, see each hourly client's unbilled hours,
    create a draft Stripe invoice per client. Retainer clients aren't listed —
    they're billed as a fixed recurring charge, not per-hour."""
    today = timezone.localdate()
    year = int(request.GET.get("year", today.year))
    month = int(request.GET.get("month", today.month))
    period_start, period_end = _period(year, month)

    hourly = Client.objects.filter(billing_mode=Client.BillingMode.HOURLY)
    previews = [billing.preview_client(c, period_start, period_end) for c in hourly]

    # Non-hourly clients (contract/retainer, on a Stripe subscription) — exempt from
    # hourly billing; shown here only so their tracked hours + subscription are visible.
    subscription = Client.objects.exclude(billing_mode=Client.BillingMode.HOURLY)
    sub_previews = [
        {"client": c, "hours": billing.logged_hours(c, period_start, period_end)}
        for c in subscription
    ]

    return render(request, "tickets/billing.html", {
        "previews": previews,
        "sub_previews": sub_previews,
        "year": year,
        "month": month,
        "month_name": period_start.strftime("%B"),
        "period_start": period_start,
        "period_end": period_end,
        "months": [(i, date(2000, i, 1).strftime("%B")) for i in range(1, 13)],
        "years": range(today.year, today.year - 4, -1),
        "recent_invoices": Invoice.objects.select_related("client")[:12],
        "stripe_configured": bool(settings.STRIPE_SECRET_KEY),
        "stripe_test_mode": settings.STRIPE_TEST_MODE,
    })


@login_required
def sync_clients(request):
    """Manual re-sync: reconcile all TimeTrax clients with Stripe (POST)."""
    if request.method != "POST":
        return redirect("tickets:billing")
    try:
        counts, errors = billing.sync_all_clients()
        if counts:
            summary = ", ".join(f"{n} {action}" for action, n in counts.items())
            messages.success(request, f"Clients synced to Stripe — {summary}.")
        else:
            messages.success(request, "No clients to sync.")
        for err in errors:
            messages.error(request, f"Sync issue — {err}")
    except billing.BillingError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"Stripe error: {e}")
    return redirect("tickets:billing")


@login_required
def create_invoice(request):
    """Create one draft invoice for a client + period (POST from the billing page)."""
    if request.method != "POST":
        return redirect("tickets:billing")

    client = get_object_or_404(Client, pk=request.POST.get("client"))
    year = int(request.POST.get("year"))
    month = int(request.POST.get("month"))
    period_start, period_end = _period(year, month)

    try:
        inv = billing.create_draft_invoice(client, period_start, period_end, user=request.user)
        messages.success(
            request,
            f"Draft invoice created for {client}: {inv.total_hours}h · ${inv.amount}. "
            "Review it in Stripe, then send.",
        )
    except billing.BillingError as e:
        messages.error(request, str(e))
    except Exception as e:  # Stripe/network error — surface it, don't 500.
        messages.error(request, f"Stripe error: {e}")

    return redirect(f"{reverse('tickets:billing')}?year={year}&month={month}")


@login_required
def comment_edit(request, pk):
    comment = get_object_or_404(Comment, pk=pk)
    if request.method == "POST":
        if "delete" in request.POST:
            item_pk = comment.item.pk
            comment.delete()
            return redirect("tickets:detail", pk=item_pk)
        form = CommentForm(request.POST, instance=comment)
        if form.is_valid():
            form.save()
            return redirect("tickets:detail", pk=comment.item.pk)
    else:
        form = CommentForm(instance=comment)
    return render(request, "tickets/entry_edit.html", {
        "form": form, "entry": comment, "kind": "Comment",
    })
