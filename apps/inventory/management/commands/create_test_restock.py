from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.inventory.models import RestockRequest, RestockLine
from apps.products.models import Product
from apps.inventory.models import SalesPoint
from decimal import Decimal

class Command(BaseCommand):
    help = 'Create a test restock request for testing validation'

    def handle(self, *args, **options):
        User = get_user_model()
        
        # Find a salespoint manager
        manager = User.objects.filter(role__in=['sales_manager', 'gerant', 'gérant']).first()
        if not manager:
            self.stdout.write(self.style.ERROR('No salespoint manager found'))
            return
        
        # Find a salespoint
        salespoint = getattr(manager, 'salespoint', None)
        if not salespoint:
            self.stdout.write(self.style.ERROR(f'Manager {manager.username} has no salespoint'))
            return
        
        # Find some products
        products = Product.objects.filter(cost_price__gt=0)[:3]
        if not products:
            self.stdout.write(self.style.ERROR('No products found with cost price'))
            return
        
        # Create test restock request
        restock_request = RestockRequest.objects.create(
            salespoint=salespoint,
            requested_by=manager,
            status='sent',  # Ready for validation
            reference=f'TEST-{salespoint.name[:2].upper()}-{restock_request.id if 'restock_request' in locals() else "001"}',
        )
        
        # Create test lines
        for i, product in enumerate(products):
            RestockLine.objects.create(
                request=restock_request,
                product=product,
                quantity_requested=i + 2,  # 2, 3, 4
                quantity_approved=i + 2,
            )
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Created test restock request {restock_request.id} for {salespoint.name} '
                f'with {len(products)} products. Status: {restock_request.status}'
            )
        )
        
        # Show details
        self.stdout.write(f'Reference: {restock_request.reference}')
        self.stdout.write(f'Manager: {manager.username}')
        self.stdout.write(f'Salespoint: {salespoint.name}')
        self.stdout.write('Products:')
        for line in restock_request.lines.all():
            self.stdout.write(f'  - {line.product.name}: {line.quantity_approved} units')

