"""VOC (VeteranOp Operations Center) — silent 6-hour maintenance.

Scans TimeTrax for stale open tickets, cross-references vault task files,
auto-closes completed tickets, and logs all actions. Designed to run
silently via Windows scheduled task (no_agent cron pattern).

Usage:
    python manage.py voc_maintenance [--dry-run]
    python manage.py voc_maintenance [--verbose]

Typical cron: every 6 hours, silent unless errors.
"""

import re
import sys
from datetime import datetime, timedelta, timezone as tz
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import OuterRef, Q, Subquery, Sum
from django.utils import timezone

from tickets.models import Comment, Item, WorkNote

# ── Paths ──────────────────────────────────────────────
PROJECTS_ROOT = Path(__file__).resolve().parents[4]
OPEN_ACTIONS_PATH = PROJECTS_ROOT / "Open-Actions.md"
AUTOMATION_BACKLOG_PATH = (
    PROJECTS_ROOT / "VeteranOp" / "Operations" / "hermes-automation-backlog.md"
)

# ── Thresholds ─────────────────────────────────────────
STALE_DAYS = 7       # no activity → flagged as stale
AUTO_CLOSE_DAYS = 14  # no activity + has work notes → close candidate
BATCH_SIZE = 50      # process items in batches


class Command(BaseCommand):
    help = "VOC silent maintenance — scan tickets, close stale completed, cross-ref vault."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be done without changing anything.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print to stdout as well as the log file.",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.verbose = options["verbose"]
        self.now = timezone.now()
        self.log_entries = []
        self.errors = []
        self.changes = {"closed": 0, "commented": 0, "flagged": 0}

        self._log(f"VOC maintenance run — {self.now.isoformat()}")
        if self.dry_run:
            self._log("  [DRY RUN — no changes will be made]")

        self._scan_stale_tickets()
        self._cross_ref_vault()
        self._auto_close_candidates()
        self._cleanup_orphans()

        self._write_log()
        self._report()
        if self.errors:
            self.stderr.write(
                self.style.ERROR(
                    f"VOC completed with {len(self.errors)} error(s). "
                    f"See {LOG_PATH}"
                )
            )
            sys.exit(1)

    # ── helpers ─────────────────────────────────────────

    def _log(self, msg: str):
        ts = self.now.strftime("[%Y-%m-%d %H:%M:%S]")
        self.log_entries.append(f"{ts} {msg}")
        if self.verbose:
            self.stdout.write(msg)

    def _msg(self, item: Item, text: str) -> str:
        return f"  {item.ticket_number} · {item.title[:60]} — {text}"

    # ── scans ───────────────────────────────────────────

    def _scan_stale_tickets(self):
        """Flag open tickets with no activity for STALE_DAYS."""
        cutoff = self.now - timedelta(days=STALE_DAYS)
        stale = (
            Item.objects.filter(status=Item.Status.OPEN)
            .annotate(
                latest_note=Subquery(
                    WorkNote.objects.filter(item=OuterRef("pk"))
                    .order_by("-created_at")
                    .values("created_at")[:1]
                )
            )
            .filter(
                Q(updated_at__lt=cutoff)
                & (
                    Q(latest_note__isnull=True)
                    | Q(latest_note__lt=cutoff)
                )
            )
        )[:BATCH_SIZE]

        count = stale.count()
        if count:
            self._log(f"Stale open tickets (>={STALE_DAYS}d no activity): {count}")
            for item in stale:
                self._log(self._msg(item, "STALE"))
            self.changes["flagged"] += count
        else:
            self._log("No stale open tickets found.")

    def _cross_ref_vault(self):
        """Cross-reference open tickets with vault task lists."""
        ticket_numbers = set(
            Item.objects.filter(status=Item.Status.OPEN).values_list(
                "ticket_number", flat=True
            )
        )

        vault_refs = {}

        # Parse Open-Actions.md
        if OPEN_ACTIONS_PATH.exists():
            text = OPEN_ACTIONS_PATH.read_text(encoding="utf-8", errors="replace")
            for num in ticket_numbers:
                if num and num in text:
                    vault_refs.setdefault(num, []).append(
                        "Open-Actions.md"
                    )

        # Parse automation backlog
        if AUTOMATION_BACKLOG_PATH.exists():
            text = AUTOMATION_BACKLOG_PATH.read_text(encoding="utf-8", errors="replace")
            for num in ticket_numbers:
                if num and num in text:
                    vault_refs.setdefault(num, []).append(
                        "hermes-automation-backlog.md"
                    )

        if vault_refs:
            self._log(
                f"Vault cross-ref: {len(vault_refs)} ticket(s) referenced in vault files"
            )
            for num, sources in sorted(vault_refs.items()):
                self._log(f"  {num} → {', '.join(sources)}")
        else:
            self._log("No open tickets referenced in vault files.")

    def _auto_close_candidates(self):
        """Close open tickets with work notes and no recent activity."""
        cutoff = self.now - timedelta(days=AUTO_CLOSE_DAYS)
        candidates = (
            Item.objects.filter(status=Item.Status.OPEN)
            .annotate(
                latest_note=Subquery(
                    WorkNote.objects.filter(item=OuterRef("pk"))
                    .order_by("-created_at")
                    .values("created_at")[:1]
                ),
                total_hours=Subquery(
                    WorkNote.objects.filter(item=OuterRef("pk"))
                    .values("item")
                    .annotate(s=Sum("hours"))
                    .values("s")
                ),
            )
            .filter(
                latest_note__isnull=False,
                latest_note__lt=cutoff,
                total_hours__gt=0,
            )
        )[:BATCH_SIZE]

        count = candidates.count()
        if count:
            self._log(
                f"Auto-close candidates (>{AUTO_CLOSE_DAYS}d idle + work logged): {count}"
            )
            for item in candidates:
                note = f"VOC auto-closed: no activity for >{AUTO_CLOSE_DAYS}d with work logged."
                self._log(self._msg(item, f"AUTO-CLOSE — {note}"))

                if not self.dry_run:
                    Comment.objects.create(
                        item=item,
                        source=Comment.Source.SYSTEM,
                        body=(
                            f"🤖 **VOC auto-close** — {self.now.isoformat()}\n\n"
                            f"{note}\n\n"
                            f"Total hours logged: {item.total_hours}"
                        ),
                    )
                    item.close()
                    self.changes["closed"] += 1
        else:
            self._log("No tickets eligible for auto-close.")

    def _cleanup_orphans(self):
        """Flag items with zero work notes opened >30d ago (never started)."""
        cutoff = self.now - timedelta(days=30)
        orphans = Item.objects.filter(
            status=Item.Status.OPEN,
            created_at__lt=cutoff,
        ).annotate(
            note_count=Subquery(
                WorkNote.objects.filter(item=OuterRef("pk"))
                .values("item")
                .annotate(c=WorkNote.objects.filter(
                    item=OuterRef("item")
                ).order_by().values("item").annotate(cnt=Sum("hours"))
                .values("cnt"))
                .values("c")
            )
        )[:BATCH_SIZE]

        # This is complex, let me simplify — just find open items with no work notes
        # that were created more than 30 days ago
        orphans = Item.objects.filter(
            status=Item.Status.OPEN,
            created_at__lt=cutoff,
        ).annotate(
            note_count=Subquery(
                WorkNote.objects.filter(item=OuterRef("pk"))
                .values("item")
                .annotate(cnt=Sum("hours"))
                .values("cnt")
            ),
        ).filter(
            Q(note_count__isnull=True) | Q(note_count=0),
        )[:BATCH_SIZE]

        orphans = list(orphans)

        if orphans:
            self._log(
                f"Orphan tickets (>30d, never started): {len(orphans)}"
            )
            for item in orphans:
                note = (
                    f"Opened {item.created_at.date()} — "
                    "no work notes logged in >30 days."
                )
                self._log(self._msg(item, f"ORPHAN — {note}"))
                if not self.dry_run:
                    Comment.objects.create(
                        item=item,
                        source=Comment.Source.SYSTEM,
                        body=(
                            f"🤖 **VOC flagged orphan** — {self.now.isoformat()}\n\n"
                            f"{note}\n\n"
                            "Consider closing or reassigning."
                        ),
                    )
                    self.changes["commented"] += 1
        else:
            self._log("No orphan tickets found.")

    # ── output ──────────────────────────────────────────

    def _write_log(self):
        """Print to stdout — the bat file captures this into the log."""
        print("\n".join(self.log_entries))
        print("-" * 60)

    def _report(self):
        self._log(
            f"VOC complete — "
            f"closed={self.changes['closed']}, "
            f"commented={self.changes['commented']}, "
            f"flagged={self.changes['flagged']}"
        )
