"""URL routing for the n8n / Hermes webhook API."""
from django.urls import path

from . import api

urlpatterns = [
    path("tickets/", api.tickets, name="api_tickets"),
    path("tickets/<int:pk>/", api.ticket_detail, name="api_ticket_detail"),
    path("tickets/<int:pk>/comments/", api.add_comment, name="api_add_comment"),
    path("clients/", api.clients, name="api_clients"),
]
