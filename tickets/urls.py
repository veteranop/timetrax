from django.urls import path

from . import views

app_name = "tickets"

urlpatterns = [
    path("", views.ticket_list, name="list"),
    path("ticket/new/", views.ticket_create, name="create"),
    path("ticket/<int:pk>/", views.ticket_detail, name="detail"),
    path("worknote/<int:pk>/edit/", views.worknote_edit, name="worknote_edit"),
    path("comment/<int:pk>/edit/", views.comment_edit, name="comment_edit"),
    path("billing/", views.billing_dashboard, name="billing"),
    path("billing/invoice/", views.create_invoice, name="create_invoice"),
    path("billing/sync-clients/", views.sync_clients, name="sync_clients"),
]
