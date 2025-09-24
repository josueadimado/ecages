from django.core.management.base import BaseCommand
from django.db import transaction
from apps.inventory.models import SalesPoint, SalesPointStock
from apps.products.models import Product

class Command(BaseCommand):
    help = "Create missing SalesPointStock for every (SalesPoint, Product). Defaults opening=0, alert=--alert."

    def add_arguments(self, parser):
        parser.add_argument("--alert", type=int, default=5, help="Default alert quantity")

    def handle(self, *args, **opts):
        default_alert = opts["alert"]
        created = 0
        with transaction.atomic():
            sps = list(SalesPoint.objects.all())
            prods = list(Product.objects.all())
            for sp in sps:
                for p in prods:
                    _, was_created = SalesPointStock.objects.get_or_create(
                        salespoint=sp, product=p,
                        defaults={"opening_qty": 0, "alert_qty": default_alert}
                    )
                    if was_created:
                        created += 1
        self.stdout.write(self.style.SUCCESS(f"Initialized {created} SalesPointStock rows (only missing)."))