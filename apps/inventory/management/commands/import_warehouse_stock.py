import csv
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.inventory.models import SalesPoint, SalesPointStock
from apps.products.models import Product


class Command(BaseCommand):
    help = "Import warehouse stock from a CSV, matching products by name (+ optional brand)."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to CSV file")
        parser.add_argument(
            "--warehouse",
            help="Warehouse salespoint name (icontains match). If omitted, uses SalesPoint(is_warehouse=True)",
        )
        parser.add_argument(
            "--update-prices",
            action="store_true",
            help="If set, updates product prices (cost/wholesale/selling) from CSV when present.",
        )
        parser.add_argument(
            "--delimiter",
            default=",",
            help="CSV delimiter (default ',').",
        )

    def _norm(self, s: str) -> str:
        return (s or "").strip().upper()

    def _to_decimal(self, v):
        try:
            return Decimal(str(v).strip())
        except Exception:
            return Decimal("0")

    def handle(self, *args, **opts):
        path = opts["file"]
        wh_name = (opts.get("warehouse") or "").strip()
        update_prices = bool(opts.get("update_prices"))
        delimiter = opts.get("delimiter") or ","

        # Resolve warehouse salespoint
        wh = None
        if wh_name:
            wh = SalesPoint.objects.filter(name__icontains=wh_name).order_by("name").first()
        if wh is None:
            wh = SalesPoint.objects.filter(is_warehouse=True).first()
        if wh is None:
            raise CommandError("No warehouse SalesPoint found. Set is_warehouse=True on your warehouse or pass --warehouse.")

        # Build product indexes for quick name/brand lookup
        products = (
            Product.objects.select_related("brand")
            .only("id", "name", "brand__name", "cost_price", "wholesale_price", "selling_price")
        )
        name_to_ids = {}
        name_brand_to_id = {}
        for p in products:
            key = self._norm(p.name)
            name_to_ids.setdefault(key, set()).add(p.id)
            kb = (self._norm(p.name), self._norm(getattr(getattr(p, "brand", None), "name", "")))
            if kb not in name_brand_to_id:
                name_brand_to_id[kb] = p.id

        created = 0
        updated = 0
        skipped = 0
        price_updates = 0
        unmatched = []

        # Read CSV
        try:
            f = open(path, newline="", encoding="utf-8")
        except Exception as e:
            raise CommandError(f"Cannot open file: {e}")
        with f:
            reader = csv.DictReader(f, delimiter=delimiter)
            # Expected headers: Name, Brand, Cost Price, Wholesale Price, Selling Price, Quantity
            for row in reader:
                name = row.get("Name") or row.get("name") or ""
                brand = row.get("Brand") or row.get("brand") or ""
                qty_str = row.get("Quantity") or row.get("Qty") or row.get("quantity") or "0"
                cost = self._to_decimal(row.get("Cost Price"))
                whp = self._to_decimal(row.get("Wholesale Price"))
                sell = self._to_decimal(row.get("Selling Price"))

                key = self._norm(name)
                kb = (self._norm(name), self._norm(brand))

                product_id = name_brand_to_id.get(kb)
                # Fallback: name-only, but only when unique
                if product_id is None:
                    ids = list(name_to_ids.get(key) or [])
                    if len(ids) == 1:
                        product_id = ids[0]

                if product_id is None:
                    unmatched.append((name, brand, qty_str))
                    skipped += 1
                    continue

                try:
                    quantity = int(str(qty_str).strip() or 0)
                except Exception:
                    quantity = 0

                with transaction.atomic():
                    sps, created_sps = SalesPointStock.objects.select_for_update().get_or_create(
                        salespoint=wh, product_id=product_id
                    )
                    # Load opening quantity from the sheet
                    sps.opening_qty = int(quantity)
                    sps.save(update_fields=["opening_qty"])
                    if created_sps:
                        created += 1
                    else:
                        updated += 1

                    if update_prices:
                        try:
                            p = Product.objects.get(pk=product_id)
                            fields_to_update = []
                            if cost and cost != p.cost_price:
                                p.cost_price = cost; fields_to_update.append("cost_price")
                            if whp and whp != p.wholesale_price:
                                p.wholesale_price = whp; fields_to_update.append("wholesale_price")
                            if sell and sell != p.selling_price:
                                p.selling_price = sell; fields_to_update.append("selling_price")
                            if fields_to_update:
                                p.save(update_fields=fields_to_update)
                                price_updates += 1
                        except Exception:
                            pass

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {created}, Updated: {updated}, Skipped (unmatched): {skipped}, Price updates: {price_updates}."
        ))
        if unmatched:
            self.stdout.write("Unmatched rows (first 20):")
            for name, brand, qty in unmatched[:20]:
                self.stdout.write(f" - {name} [{brand}] x {qty}")

