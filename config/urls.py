"""URL configuration for config project."""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="tickets/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("tickets.urls")),
    # n8n / Hermes webhook API
    path("api/", include("tickets.api_urls")),
]
