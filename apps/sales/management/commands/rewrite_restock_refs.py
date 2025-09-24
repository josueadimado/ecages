from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from apps.inventory.models import RestockRequest


class Command(BaseCommand):
    help = (
        "Rewrite existing salespoint → warehouse restock references to the new format WH-RQ-DDMMYY-0001."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Show changes without saving"
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Limit rows to process (0 = all)"
        )

    def handle(self, *args, **options):
        dry = bool(options.get("dry_run"))
        limit = int(options.get("limit") or 0)

        # Target anything not already in WH-RQ- format
        qs = RestockRequest.objects.exclude(reference__startswith="WH-RQ-").exclude(reference__isnull=True).exclude(reference="").order_by("created_at")
        if limit > 0:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No references to rewrite."))
            return

        self.stdout.write(f"Found {total} references to rewrite. Processing…")

        updated = 0
        for req in qs:
            created_day = timezone.localtime(req.created_at).date() if req.created_at else timezone.localdate()
            prefix = f"WH-RQ-{created_day.strftime('%d%m%y')}-"
            with transaction.atomic():
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
                if dry:
                    self.stdout.write(f"Would set {req.id} {req.reference} → {new_ref}")
                else:
                    req.reference = new_ref
                    req.save(update_fields=["reference"])
                    updated += 1

        if dry:
            self.stdout.write(self.style.WARNING("Dry-run complete. No changes saved."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Rewrite complete. Updated {updated} reference(s)."))




