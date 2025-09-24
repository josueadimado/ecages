# apps/products/management/commands/import_products_from_excel.py
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.apps import apps

try:
    import pandas as pd
except ImportError:
    raise CommandError("Please `pip install pandas openpyxl` to import from Excel/CSV.")

Provider = apps.get_model("providers", "Provider")
Product  = apps.get_model("products", "Product")

# Try to locate Brand (prefer providers.Brand, else products.Brand). Optional.
Brand = None
try:
    Brand = apps.get_model("providers", "Brand")
except Exception:
    try:
        Brand = apps.get_model("products", "Brand")
    except Exception:
        Brand = None


def _normalize_type(raw, name_fallback: str) -> str:
    """
    Normalize product type. Accepts values like 'moto', 'Moto', 'pièce', 'piece', etc.
    If empty/unknown, fallback to name-based rule (contains 'moto' -> 'moto', else 'piece').
    """
    if raw is not None:
        s = str(raw).strip().lower()
        if s in {"moto", "motorcycle", "motor", "motos"}:
            return "moto"
        if s in {"piece", "pièce", "pieces", "pièces", "spare", "spares"}:
            return "piece"
    return "moto" if "moto" in (name_fallback or "").lower() else "piece"


class Command(BaseCommand):
    help = (
        "Import products from Excel/CSV. Upserts by (name, provider). "
        "Auto-creates Providers and Brands (brand is scoped by provider if model supports it). "
        "Also normalizes product_type from a 'Type' column or by the product name (contains 'moto' => Moto)."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to Excel (.xlsx) or CSV.")
        parser.add_argument("--sheet", default=None, help="Excel sheet name (default: first).")
        parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write to DB.")
        parser.add_argument(
            "--encoding",
            default="utf-8",
            help="CSV encoding if CSV input (e.g. cp1252 or latin1).",
        )

    def _load_df(self, path, sheet=None, encoding="utf-8"):
        path = str(path)
        if path.lower().endswith(".xlsx"):
            xls = pd.ExcelFile(path)
            sheet = sheet or xls.sheet_names[0]
            return xls.parse(sheet)
        elif path.lower().endswith(".csv"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError as e:
                raise CommandError(
                    f"Failed to read CSV with encoding '{encoding}'. "
                    f"Try e.g. --encoding=cp1252 or --encoding=latin1. Details: {e}"
                )
        else:
            raise CommandError("Unsupported file type. Use .xlsx or .csv")

    def handle(self, *args, **opts):
        path  = opts["path"]
        sheet = opts["sheet"]
        dry   = opts["dry_run"]
        enc   = opts["encoding"]

        df = self._load_df(path, sheet, enc)

        # Map French headers → normalized names if needed
        rename_map = {
            "Produits": "name",
            "Marque": "brand",
            "Fournisseur": "provider",
            "Prix d'achat": "cost_price",
            "Prix en Gros": "wholesale_price",
            "Prix detail": "retail_price",
            "Type": "type",  # NEW: capture product type column
        }
        for src, dst in rename_map.items():
            if src in df.columns and dst not in df.columns:
                df[dst] = df[src]

        # Required
        for col in ["name", "provider"]:
            if col not in df.columns:
                raise CommandError(f"Missing required column '{col}'. Present: {list(df.columns)}")

        # Optional columns (create if missing to simplify code)
        for c in [
            "brand",
            "cost_price",
            "wholesale_price",
            "retail_price",
            "sale_price",
            "selling_price",
            "unit_price",
            "price",
            "type",
        ]:
            if c not in df.columns:
                df[c] = None

        # Clean/normalize strings (avoid 'nan' by fillna('') first)
        for col in ("name", "provider", "brand", "type"):
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str).str.strip()

        # Numeric price columns
        money_cols = ["cost_price", "wholesale_price", "retail_price", "sale_price", "selling_price", "unit_price", "price"]
        for c in money_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        # Introspect Product fields to only set what exists
        product_fields = {f.name for f in Product._meta.get_fields()}
        has_brand_fk   = "brand" in product_fields
        has_cost       = "cost_price" in product_fields
        has_wholesale  = "wholesale_price" in product_fields
        has_type       = "product_type" in product_fields
        has_active     = "is_active" in product_fields

        # Decide which field represents the selling price on your Product
        selling_candidates = ["retail_price", "sale_price", "selling_price", "unit_price", "price"]
        selling_field = next((f for f in selling_candidates if f in product_fields), None)

        # Is Brand scoped by provider?
        brand_scoped_by_provider = False
        if Brand is not None:
            try:
                brand_fields = {f.name for f in Brand._meta.get_fields()}
                brand_scoped_by_provider = "provider" in brand_fields
            except Exception:
                brand_scoped_by_provider = False

        created_products = updated_products = created_providers = created_brands = 0

        with transaction.atomic():
            for _, row in df.iterrows():
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                provider_name = (row.get("provider") or "").strip()
                if not provider_name:
                    continue

                # Provider (required)
                provider, prov_created = Provider.objects.get_or_create(name=provider_name)
                if prov_created:
                    created_providers += 1

                # Brand (optional)
                brand_obj = None
                brand_name = (row.get("brand") or "").strip()
                if Brand is not None and brand_name:
                    if brand_scoped_by_provider:
                        brand_obj, brand_created = Brand.objects.get_or_create(name=brand_name, provider=provider)
                    else:
                        brand_obj, brand_created = Brand.objects.get_or_create(name=brand_name)
                    if brand_created:
                        created_brands += 1

                # Build defaults for Product (only fields that exist)
                defaults = {"provider": provider}

                if has_brand_fk:
                    defaults["brand"] = brand_obj

                if has_cost and pd.notna(row.get("cost_price")):
                    defaults["cost_price"] = Decimal(str(row.get("cost_price")))

                if has_wholesale and pd.notna(row.get("wholesale_price")):
                    defaults["wholesale_price"] = Decimal(str(row.get("wholesale_price")))

                if selling_field:
                    for src in selling_candidates:
                        val = row.get(src)
                        if pd.notna(val):
                            defaults[selling_field] = Decimal(str(val))
                            break

                # NEW: product_type
                if has_type:
                    defaults["product_type"] = _normalize_type(row.get("type"), name)

                # Optional: mark active on import
                if has_active:
                    defaults["is_active"] = True

                # Upsert strictly by (name + provider)
                obj, created = Product.objects.update_or_create(
                    name=name,
                    provider=provider,
                    defaults=defaults
                )
                if created:
                    created_products += 1
                else:
                    updated_products += 1

            if dry:
                # rollback intentionally with a summary
                raise CommandError(
                    f"[DRY-RUN] Would create={created_products}, update={updated_products}, "
                    f"providers_created={created_providers}, brands_created={created_brands}"
                )

        self.stdout.write(self.style.SUCCESS(
            f"Imported OK. created={created_products}, updated={updated_products}, "
            f"providers_created={created_providers}, brands_created={created_brands}"
        ))