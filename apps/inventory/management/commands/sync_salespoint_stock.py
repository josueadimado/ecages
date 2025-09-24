# apps/inventory/management/commands/sync_salespoint_stock.py
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.apps import apps

Product = apps.get_model("products", "Product")
SalesPoint = apps.get_model("inventory", "SalesPoint")
SalesPointStock = apps.get_model("inventory", "SalesPointStock")


class Command(BaseCommand):
    help = (
        "Create missing SalesPointStock rows for a given sales point.\n"
        "By default, uses the sales point's brand if set. "
        "Use --source=all to ignore brand. Use --reset to wipe its current stock first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--salespoint-id", type=int, help="SalesPoint ID")
        parser.add_argument("--salespoint", help="SalesPoint name (if you prefer name over id)")
        parser.add_argument("--source", choices=["brand", "all"], default="brand",
                            help="Where to pull products from: 'brand' (default) or 'all'.")
        parser.add_argument("--only", choices=["piece", "moto", "both"], default="both",
                            help="Restrict by product_type. Default: both.")
        parser.add_argument("--reset", action="store_true",
                            help="Delete existing SalesPointStock rows for this sales point before loading.")
        parser.add_argument("--alert", type=int, default=5,
                            help="Default alert quantity per product. Default: 5.")
        parser.add_argument("--opening", type=int, default=0,
                            help="Default opening_qty for newly created rows. Default: 0.")

    def _resolve_salespoint(self, sid, sname):
        if sid:
            try:
                return SalesPoint.objects.get(pk=sid)
            except SalesPoint.DoesNotExist:
                raise CommandError(f"SalesPoint id={sid} not found.")
        if sname:
            try:
                return SalesPoint.objects.get(name=sname)
            except SalesPoint.DoesNotExist:
                raise CommandError(f"SalesPoint name='{sname}' not found.")
        raise CommandError("Provide --salespoint-id or --salespoint.")

    def handle(self, *args, **opts):
        sp = self._resolve_salespoint(opts.get("salespoint_id"), opts.get("salespoint"))
        source = opts["source"]
        only   = opts["only"]
        reset  = opts["reset"]
        alert  = opts["alert"]
        opening = opts["opening"]

        # Build product queryset
        qs = Product.objects.filter(is_active=True)
        if source == "brand":
            if sp.brand_id:
                qs = qs.filter(brand_id=sp.brand_id)
            else:
                self.stdout.write(self.style.WARNING(
                    f"SalesPoint '{sp.name}' has no brand set; falling back to ALL products."
                ))
        if only in ("piece", "moto"):
            qs = qs.filter(product_type=only)

        # Optional: nudge performance (indexes recommended but not required)
        qs = qs.only("id")  # we only need IDs to create links

        # Optionally reset
        if reset:
            deleted, _ = SalesPointStock.objects.filter(salespoint=sp).delete()
            self.stdout.write(self.style.WARNING(f"Deleted existing rows: {deleted}"))

        # Create missing rows
        existing_ids = set(
            SalesPointStock.objects.filter(salespoint=sp).values_list("product_id", flat=True)
        )
        to_create = []
        for pid in qs.values_list("id", flat=True).iterator(chunk_size=5000):
            if pid not in existing_ids:
                to_create.append(SalesPointStock(
                    salespoint=sp,
                    product_id=pid,
                    opening_qty=opening,
                    sold_qty=0,
                    transfer_in=0,
                    transfer_out=0,
                    alert_qty=alert,
                ))

        created = 0
        with transaction.atomic():
            # ignore_conflicts=True respects the unique(salespoint, product) constraint
            for i in range(0, len(to_create), 1000):
                batch = to_create[i:i+1000]
                SalesPointStock.objects.bulk_create(batch, ignore_conflicts=True)
                created += len(batch)

        total = SalesPointStock.objects.filter(salespoint=sp).count()
        self.stdout.write(self.style.SUCCESS(
            f"Sync OK for '{sp.name}'. created={created}, total_now={total}, "
            f"source={source}, filter={only}, alert={alert}, opening={opening}"
        ))