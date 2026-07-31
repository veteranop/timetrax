from django.apps import AppConfig


class TicketsConfig(AppConfig):
    name = 'tickets'

    def ready(self):
        # Register the Client → Stripe bonding signal.
        from . import signals  # noqa: F401
