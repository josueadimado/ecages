# apps/sales/management/commands/recalc_sales_totals.py
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from apps.sales.models import Sale

class Command(BaseCommand):
    help = (
        "Recompute SaleItem.line_total for all items (optional) and "
        "recalculate Sale.total_amount for the selected sales. "
        "Can also set received_amount to total for approved sales."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change without saving.")
        parser.add_argument(
            "--status",
            nargs="*",
            choices=["draft", "awaiting_cashier", "approved", "rejected", "cancelled"],
            help="Limit to sales having one of these statuses.",
        )
        parser.add_argument("--ids", nargs="*", type=int, help="Limit to specific sale IDs.")
        parser.add_argument("--batch-size", type=int, default=500, help="DB iteration chunk size.")
        parser.add_argument(
            "--fix-items",
            action="store_true",
            help="Also recompute line_total for each SaleItem as unit_price * quantity.",
        )
        parser.add_argument(
            "--set-received-to-total",
            action="store_true",
            help="If a sale is approved and received_amount is NULL, set it to total_amount.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        batch_size = options["batch_size"]

        qs = Sale.objects.all().select_related("salespoint", "seller").prefetch_related("items")
        if options.get("status"):
            qs = qs.filter(status__in=options["status"])
        if options.get("ids"):
            qs = qs.filter(id__in=options["ids"])

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No sales matched your filters."))
            return

        self.stdout.write(f"Processing {total} sale(s)... (dry_run={dry})")

        sales_changed = 0
        items_changed = 0

        for sale in qs.iterator(chunk_size=batch_size):
            with transaction.atomic():
                fields_to_update = []
                # Optionally recompute item line totals
                if options["fix_items"]:
                    for it in sale.items.all():
                        correct = (it.unit_price or Decimal("0")) * Decimal(it.quantity or 0)
                        if it.line_total != correct:
                            items_changed += 1
                            if not dry:
                                it.line_total = correct
                                it.save(update_fields=["line_total"])

                # Always recalc sale total from items (method already sets sale.total_amount)
                new_total = sale.recalc_total()

                if sale.total_amount != new_total:
                    fields_to_update.append("total_amount")

                # Optionally set received_amount for approved sales when missing
                if options["set_received_to_total"] and sale.status == "approved" and sale.received_amount is None:
                    sale.received_amount = sale.total_amount
                    fields_to_update.append("received_amount")

                if fields_to_update:
                    sales_changed += 1
                    if not dry:
                        sale.save(update_fields=list(set(fields_to_update)))

        msg = (
            f"Done. Sales updated: {sales_changed} | "
            f"Item line_totals updated: {items_changed} | "
            f"Scanned: {total}"
        )
        if dry:
            msg += " (dry run, nothing saved)"
        self.stdout.write(self.style.SUCCESS(msg))