from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
import pandas as pd

from apps.inventory.models import SalesPoint, SalesPointStock
from apps.products.models import Product

class Command(BaseCommand):
    help = "Bulk update SalesPointStock from CSV/XLSX with columns: salespoint, product, opening_qty[, alert_qty]."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to CSV/XLSX")
        parser.add_argument("--sheet", default=None)
        parser.add_argument("--encoding", default="utf-8")

    def _load_df(self, path, sheet=None, encoding="utf-8"):
        if path.lower().endswith(".xlsx"):
            xls = pd.ExcelFile(path)
            return xls.parse(sheet or xls.sheet_names[0])
        elif path.lower().endswith(".csv"):
            return pd.read_csv(path, encoding=encoding)
        else:
            raise CommandError("Unsupported file type (use .csv or .xlsx)")

    def handle(self, *args, **opts):
        path, sheet, enc = opts["path"], opts["sheet"], opts["encoding"]
        df = self._load_df(path, sheet, enc)

        required = ["salespoint", "product", "opening_qty"]
        for col in required:
            if col not in df.columns:
                raise CommandError(f"Missing required column: {col}")

        df["salespoint"]  = df["salespoint"].astype(str).str.strip()
        df["product"]     = df["product"].astype(str).str.strip()
        df["opening_qty"] = pd.to_numeric(df["opening_qty"], errors="coerce").fillna(0).astype(int)
        if "alert_qty" not in df.columns:
            df["alert_qty"] = None

        created = updated = not_found = 0
        with transaction.atomic():
            for _, row in df.iterrows():
                sp_name = row["salespoint"]
                prod_name = row["product"]
                qty = int(row["opening_qty"])
                alert = None
                if row.get("alert_qty") is not None and str(row.get("alert_qty")).strip() != "":
                    try:
                        alert = int(row["alert_qty"])
                    except Exception:
                        alert = None

                try:
                    sp = SalesPoint.objects.get(name__iexact=sp_name)
                    p  = Product.objects.get(name__iexact=prod_name)
                except (SalesPoint.DoesNotExist, Product.DoesNotExist):
                    not_found += 1
                    continue

                obj, created_row = SalesPointStock.objects.get_or_create(
                    salespoint=sp, product=p, defaults={"opening_qty": qty}
                )
                if created_row:
                    created += 1
                else:
                    obj.opening_qty = qty
                if alert is not None:
                    obj.alert_qty = alert
                obj.save()
                if not created_row:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Stock imported: created={created}, updated={updated}, not_found_pairs={not_found}"
        ))