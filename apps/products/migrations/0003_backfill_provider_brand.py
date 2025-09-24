from django.db import migrations

def backfill_provider_brand(apps, schema_editor):
    Provider = apps.get_model('providers', 'Provider')
    Brand = apps.get_model('providers', 'Brand')
    Product = apps.get_model('products', 'Product')

    # Get or create defaults
    provider, _ = Provider.objects.get_or_create(name="Default Provider", defaults={
        "is_active": True,
    })
    brand, _ = Brand.objects.get_or_create(name="Default Brand", provider=provider)

    # Fill missing provider / brand
    Product.objects.filter(provider__isnull=True).update(provider=provider)
    Product.objects.filter(brand__isnull=True).update(brand=brand)

class Migration(migrations.Migration):

    dependencies = [
        ('providers', '0001_initial'),   # adjust if your providers first migration is different
        ('products', '0002_rename_retail_price_product_cost_price_and_more'),       # <-- replace with the actual name Django just generated for step A
    ]

    operations = [
        migrations.RunPython(backfill_provider_brand, reverse_code=migrations.RunPython.noop),
    ]