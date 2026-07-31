from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils import timezone
from django.utils.html import format_html

from .models import Client, Comment, Invoice, Item, User, WorkNote


def _status_badge(status, label):
    """Pill badge: green = Open, grey = Closed."""
    color = "#2e7d32" if status == Item.Status.OPEN else "#9e9e9e"
    return format_html(
        '<span style="display:inline-block;padding:2px 12px;border-radius:11px;'
        'background:{};color:#fff;font-weight:600;font-size:11px;'
        'letter-spacing:.4px;text-transform:uppercase;">{}</span>',
        color, label,
    )


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "role", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("VeteranOp", {"fields": ("role",)}),
    )


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "billing_mode", "hourly_rate", "stripe_subscription_id",
                    "contact_name", "stripe_customer_id")
    list_filter = ("billing_mode",)
    search_fields = ("name", "contact_name", "contact_email")
    readonly_fields = ("stripe_customer_id",)  # subscription id is editable — you paste it in


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("stripe_invoice_id", "client", "period_start", "period_end",
                    "total_hours", "amount", "status", "created_at")
    list_filter = ("status", "client")
    search_fields = ("stripe_invoice_id",)
    readonly_fields = ("stripe_invoice_id", "client", "period_start", "period_end",
                       "total_hours", "amount", "hosted_invoice_url", "created_by", "created_at")


class WorkNoteInline(admin.TabularInline):
    model = WorkNote
    extra = 1
    fields = ("body", "hours", "author", "created_at")
    readonly_fields = ("created_at",)


class CommentInline(admin.TabularInline):
    model = Comment
    extra = 0
    fields = ("source", "body", "author", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("ticket_number", "priority", "title", "client", "status_flag", "assignee", "total_hours", "updated_at")
    list_filter = ("priority", "status", "client")
    search_fields = ("ticket_number", "title", "description")
    autocomplete_fields = ("client", "related")
    readonly_fields = ("status_flag", "activity", "created_at", "updated_at", "closed_at", "total_hours")
    inlines = [WorkNoteInline, CommentInline]

    @admin.display(description="Status", ordering="status")
    def status_flag(self, obj):
        return _status_badge(obj.status, obj.get_status_display())

    @admin.display(description="Total hours")
    def total_hours(self, obj):
        return obj.total_hours

    @admin.display(description="Activity — all notes & comments")
    def activity(self, obj):
        # Merge work notes and comments into one chronological timeline.
        events = [("work", wn.created_at, wn) for wn in obj.work_notes.all()]
        events += [("comment", c.created_at, c) for c in obj.comments.all()]
        events.sort(key=lambda e: e[1])

        html = format_html(
            '<div style="margin:2px 0 10px;font-size:13px;">'
            '<b>Total logged:</b> {}h &nbsp;·&nbsp; <b>{}</b> entries</div>',
            obj.total_hours, len(events),
        )
        if not events:
            return html + format_html('<div style="color:#888;">No activity yet.</div>')

        for kind, ts, e in events:
            when = timezone.localtime(ts).strftime("%b %d, %Y · %I:%M %p")
            author = getattr(e, "author", None) or "—"
            if kind == "work":
                tag = format_html(
                    '<span style="color:#1565c0;font-weight:600;">Work note · {}h</span>',
                    e.hours,
                )
                accent = "#1565c0"
            else:
                tag = format_html(
                    '<span style="color:#6a1b9a;font-weight:600;">Comment · {}</span>',
                    e.get_source_display(),
                )
                accent = "#6a1b9a"
            html += format_html(
                '<div style="border-left:3px solid {};padding:5px 12px;margin:8px 0;'
                'background:rgba(0,0,0,.02);">'
                '<div style="font-size:11px;color:#888;">{} &nbsp;·&nbsp; {} &nbsp;·&nbsp; {}</div>'
                '<div style="margin-top:3px;white-space:pre-wrap;">{}</div></div>',
                accent, tag, author, when, e.body,
            )
        return html


@admin.register(WorkNote)
class WorkNoteAdmin(admin.ModelAdmin):
    list_display = ("item", "hours", "author", "created_at")
    search_fields = ("body",)


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("item", "source", "author", "created_at")
    list_filter = ("source",)
