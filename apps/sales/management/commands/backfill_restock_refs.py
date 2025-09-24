from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from apps.inventory.models import RestockRequest


class Command(BaseCommand):
    help = (
        "Backfill missing reference numbers for salespoint → warehouse restock requests "
        "using the format WH-DDMMYY-P-0001 (sequenced per day)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without saving",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional limit of rows to process (0 = no limit)",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        limit = int(options.get("limit") or 0)

        qs = RestockRequest.objects.filter(Q(reference__isnull=True) | Q(reference="")).order_by("created_at")

        if limit > 0:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No missing references found. Nothing to do."))
            return

        self.stdout.write(f"Found {total} restock requests without reference. Processing…")

        updated = 0
        for req in qs:
            created_day = timezone.localtime(req.created_at).date() if req.created_at else timezone.localdate()
            prefix = f"WH-{created_day.strftime('%d%m%y')}-P-"
            with transaction.atomic():
                # Compute next sequence for that day, locking existing refs with same prefix
                max_seq = 0
                for ref in (
                    RestockRequest.objects.select_for_update()
                    .filter(reference__startswith=prefix)
                    .values_list("reference", flat=True)
                ):
                    try:
                        num = int(str(ref).split("-")[-1])
                    except Exception:
                        num = 0
                    max_seq = max(max_seq, num)
                new_ref = f"{prefix}{max_seq + 1:04d}"

                if dry_run:
                    self.stdout.write(f"Would set REQ {req.id} → {new_ref}")
                else:
                    req.reference = new_ref
                    req.save(update_fields=["reference"])
                    updated += 1

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run complete. No changes saved."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Backfill complete. Updated {updated} request(s)."))


