"""Client → Stripe bonding on add, and Ticket → n8n triage trigger.

TimeTrax is the source of truth for clients (decided 2026-07-14). When a Client is
created, bond it to a Stripe Customer so the two stay married. Runs AFTER the DB
commit (never blocks the save) and swallows errors — the manual 'Sync clients'
re-sync reconciles anything that didn't go through (e.g. offline, or no key set).

When a ticket (Item) is created, fire a webhook to the local n8n instance for
AI-powered triage (classification, priority assessment, suggested action).
"""

import json
import logging
import os
import urllib.request
import urllib.error

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import billing
from .models import Client, Item

logger = logging.getLogger(__name__)

# ── n8n AI Triage Webhook ─────────────────────────────────────────────

N8N_WEBHOOK_URL = os.environ.get(
    "N8N_TICKET_WEBHOOK",
    "http://127.0.0.1:5678/webhook/timetrax-triage",
)


@receiver(post_save, sender=Item)
def notify_n8n_triage(sender, instance, created, **kwargs):
    """Fire a webhook to n8n when a new ticket is created, for AI triage."""
    if not created:
        return

    payload = {
        "ticket_id": instance.id,
        "ticket_number": instance.ticket_number,
        "title": instance.title,
        "description": instance.description or "",
        "priority": instance.priority,
        "status": instance.status,
        "client_id": instance.client_id,
        "client_name": instance.client.name if instance.client else "",
        "assignee": str(instance.assignee) if instance.assignee else "",
        "created_at": instance.created_at.isoformat() if instance.created_at else "",
    }

    def _fire():
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                N8N_WEBHOOK_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            logger.info("n8n triage notified for ticket %s", instance.ticket_number)
        except Exception:
            logger.warning(
                "n8n triage webhook failed for ticket %s (n8n down?)",
                instance.ticket_number,
            )

    transaction.on_commit(_fire)


# ── Client → Stripe bonding ──────────────────────────────────────────


@receiver(post_save, sender=Client)
def bond_client_to_stripe(sender, instance, created, **kwargs):
    if not created or not settings.STRIPE_SECRET_KEY:
        return

    def _bond():
        try:
            billing.sync_client_to_stripe(instance)
        except Exception:
            pass

    transaction.on_commit(_bond)
