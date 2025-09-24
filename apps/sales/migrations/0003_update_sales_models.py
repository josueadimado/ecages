# apps/sales/migrations/0003_update_sales_models.py
from django.db import migrations, models
from django.utils import timezone


def backfill_numbers_and_defaults(apps, schema_editor):
    Sale = apps.get_model("sales", "Sale")
    SaleItem = apps.get_model("sales", "SaleItem")

    def sp_initials(name: str) -> str:
        letters = "".join(ch for ch in (name or "") if ch.isalpha())
        return (letters[:2] or "SP").upper()

    counters = {}  # base -> last seq

    qs = Sale.objects.filter(number__isnull=True).select_related("salespoint")
    for sale in qs.iterator():
        sp = sale.salespoint
        created = sale.created_at or timezone.now()
        base_ini = sp_initials(getattr(sp, "name", ""))
        day = created.date()

        # Guess kind: 'M' if first item is a moto, else 'P'
        kind = "P"
        try:
            first_item = (
                SaleItem.objects.filter(sale_id=sale.id)
                .select_related("product")
                .first()
            )
            if first_item:
                p = first_item.product
                ptype = (getattr(p, "product_type", "") or "").lower()
                if ptype == "moto" or "moto" in (p.name or "").lower():
                    kind = "M"
        except Exception:
            pass

        base = f"{base_ini}-{day.strftime('%d%m%y')}-{kind}"
        seq = counters.get(base, 0) + 1
        counters[base] = seq
        sale.number = f"{base}-{seq:04d}"

        if not sale.payment_type:
            sale.payment_type = "cash"
        if not sale.customer_name:
            sale.customer_name = "DIVERS"
        if sale.customer_phone is None:
            sale.customer_phone = ""
        if not sale.status:
            sale.status = "pending"

        sale.save(
            update_fields=[
                "number",
                "payment_type",
                "customer_name",
                "customer_phone",
                "status",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0002_sale_saleitem_delete_tempmodel"),
    ]

    operations = [
        # 1) Add fields relaxed (number nullable, not unique yet)
        migrations.AddField(
            model_name="sale",
            name="number",
            field=models.CharField(max_length=40, null=True, db_index=True),
        ),
        migrations.AddField(
            model_name="sale",
            name="status",
            field=models.CharField(
                max_length=16,
                choices=[("pending", "En attente caisse"), ("validated", "Validée"), ("cancelled", "Annulée")],
                default="pending",
                db_index=True,
            ),
        ),
        migrations.AddField(
            model_name="sale",
            name="payment_type",
            field=models.CharField(
                max_length=16,
                choices=[("cash", "Espèce"), ("mobile", "Mobile Money"), ("card", "Carte")],
                default="cash",
            ),
        ),
        migrations.AddField(
            model_name="sale",
            name="customer_name",
            field=models.CharField(max_length=120, default="DIVERS"),
        ),
        migrations.AddField(
            model_name="sale",
            name="customer_phone",
            field=models.CharField(max_length=40, default="", blank=True),
        ),

        # 2) Backfill
        migrations.RunPython(backfill_numbers_and_defaults, migrations.RunPython.noop),

        # 3) Lock constraints (number not null + unique)
        migrations.AlterField(
            model_name="sale",
            name="number",
            field=models.CharField(max_length=40, unique=True, null=False, db_index=True),
        ),
    ]