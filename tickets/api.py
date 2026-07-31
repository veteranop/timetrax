"""n8n / Hermes webhook API for TimeTrax — ticket CRUD over plain JSON.

No DRF dependency. Auth via shared API key in Authorization header.
Supports everything n8n needs to auto-create/close/comment on tickets.
"""

import json
import os

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Client, Comment, Item, User


# ── Auth ────────────────────────────────────────────────────────────────────

_API_KEY = os.environ.get("TIMETRAX_API_KEY", "")


def _auth_ok(request) -> bool:
    """Bearer token in Authorization header matches TIMETRAX_API_KEY."""
    if not _API_KEY:
        return False
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {_API_KEY}"


def _unauthorized():
    return JsonResponse({"error": "Unauthorized — set TIMETRAX_API_KEY env var and send Bearer token"}, status=401)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _item_json(item: Item) -> dict:
    return {
        "id": item.id,
        "ticket_number": item.ticket_number,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "priority": item.priority,
        "client": {"id": item.client_id, "name": item.client.name},
        "assignee": item.assignee.username if item.assignee else None,
        "created_by": item.created_by.username if item.created_by else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "closed_at": item.closed_at.isoformat() if item.closed_at else None,
        "total_hours": float(item.total_hours),
    }


def _client_json(client: Client) -> dict:
    return {
        "id": client.id,
        "name": client.name,
        "billing_mode": client.billing_mode,
        "contact_name": client.contact_name,
        "contact_email": client.contact_email,
    }


# ── Endpoints ───────────────────────────────────────────────────────────────


@csrf_exempt
@require_http_methods(["GET", "POST"])
def tickets(request):
    """GET /api/tickets/ — list tickets (filter: ?status=open&client_id=1)
    POST /api/tickets/ — create a ticket"""
    if not _auth_ok(request):
        return _unauthorized()

    if request.method == "GET":
        items = Item.objects.select_related("client", "assignee").all()
        status = request.GET.get("status")
        if status:
            items = items.filter(status=status)
        client_id = request.GET.get("client_id")
        if client_id:
            items = items.filter(client_id=client_id)
        priority = request.GET.get("priority")
        if priority:
            items = items.filter(priority=priority.upper())
        return JsonResponse({"tickets": [_item_json(i) for i in items[:100]]})

    # POST — create ticket
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    client = Client.objects.filter(id=body.get("client_id")).first()
    if not client:
        return JsonResponse({"error": "client_id is required and must exist"}, status=400)

    assignee = None
    if body.get("assignee"):
        assignee = User.objects.filter(username=body["assignee"]).first()

    created_by = None
    if body.get("created_by"):
        created_by = User.objects.filter(username=body["created_by"]).first()

    item = Item.objects.create(
        client=client,
        title=body.get("title", ""),
        description=body.get("description", ""),
        priority=body.get("priority", "M"),
        status=body.get("status", "open"),
        assignee=assignee,
        created_by=created_by,
    )
    return JsonResponse(_item_json(item), status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
def ticket_detail(request, pk):
    """GET /api/tickets/<id>/ — get ticket
    PATCH /api/tickets/<id>/ — update (close, reassign, change priority)"""
    if not _auth_ok(request):
        return _unauthorized()

    try:
        item = Item.objects.select_related("client", "assignee").get(pk=pk)
    except Item.DoesNotExist:
        return JsonResponse({"error": "Ticket not found"}, status=404)

    if request.method == "GET":
        return JsonResponse(_item_json(item))

    # PATCH
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if "title" in body:
        item.title = body["title"]
    if "description" in body:
        item.description = body["description"]
    if "priority" in body:
        item.priority = body["priority"].upper()
    if "status" in body:
        if body["status"] == "closed" and item.status != "closed":
            item.close()
        elif body["status"] == "open" and item.status != "open":
            item.reopen()
        else:
            item.status = body["status"]
    if "assignee" in body:
        item.assignee = User.objects.filter(username=body["assignee"]).first()
    item.save()
    return JsonResponse(_item_json(item))


@csrf_exempt
@require_http_methods(["POST"])
def add_comment(request, pk):
    """POST /api/tickets/<id>/comments/ — add a comment (Hermes logs here)"""
    if not _auth_ok(request):
        return _unauthorized()

    try:
        item = Item.objects.get(pk=pk)
    except Item.DoesNotExist:
        return JsonResponse({"error": "Ticket not found"}, status=404)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    author = None
    if body.get("author"):
        author = User.objects.filter(username=body["author"]).first()

    comment = Comment.objects.create(
        item=item,
        author=author,
        body=body.get("body", ""),
        source=body.get("source", "hermes"),
    )
    return JsonResponse({
        "id": comment.id,
        "body": comment.body,
        "source": comment.source,
        "author": comment.author.username if comment.author else None,
        "created_at": comment.created_at.isoformat(),
    }, status=201)


@csrf_exempt
@require_http_methods(["GET"])
def clients(request):
    """GET /api/clients/ — list all clients"""
    if not _auth_ok(request):
        return _unauthorized()
    return JsonResponse({"clients": [_client_json(c) for c in Client.objects.all()]})
