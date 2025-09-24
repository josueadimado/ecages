"""
Management command to generate barcodes for all products.
This creates QR codes that can be scanned to identify products and locations.
"""
import os
import qrcode
from io import BytesIO
from django.core.management.base import BaseCommand
from django.conf import settings
from apps.products.models import Product
from apps.inventory.models import SalesPoint


class Command(BaseCommand):
    help = 'Generate QR codes for all products and salespoints'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output-dir',
            type=str,
            default='static/barcodes',
            help='Directory to save barcode images (default: static/barcodes)'
        )
        parser.add_argument(
            '--format',
            type=str,
            choices=['qr', 'code128'],
            default='qr',
            help='Barcode format (default: qr)'
        )
        parser.add_argument(
            '--size',
            type=int,
            default=200,
            help='Barcode image size in pixels (default: 200)'
        )

    def handle(self, *args, **options):
        output_dir = options['output_dir']
        barcode_format = options['format']
        size = options['size']
        
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate barcodes for products
        products = Product.objects.filter(is_active=True)
        self.stdout.write(f'Generating {barcode_format.upper()} codes for {products.count()} products...')
        
        for product in products:
            # Create barcode data: PROD-{product_id}-{location_id}-{batch}
            # For now, we'll generate one barcode per product (not per location)
            barcode_data = f"PROD-{product.id}-{product.sku or 'N/A'}-{product.name[:20]}"
            
            if barcode_format == 'qr':
                self._generate_qr_code(barcode_data, product, output_dir, size)
            else:
                self._generate_code128(barcode_data, product, output_dir, size)
        
        # Generate barcodes for salespoints
        salespoints = SalesPoint.objects.all()
        self.stdout.write(f'Generating {barcode_format.upper()} codes for {salespoints.count()} salespoints...')
        
        for sp in salespoints:
            barcode_data = f"SP-{sp.id}-{sp.name[:20]}"
            
            if barcode_format == 'qr':
                self._generate_qr_code(barcode_data, sp, output_dir, size, prefix='sp')
            else:
                self._generate_code128(barcode_data, sp, output_dir, size, prefix='sp')
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully generated {barcode_format.upper()} codes in {output_dir}/'
            )
        )

    def _generate_qr_code(self, data, obj, output_dir, size, prefix='prod'):
        """Generate QR code for the given data and object."""
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        # Create image
        img = qr.make_image(fill_color="black", back_color="white")
        img = img.resize((size, size))
        
        # Create safe filename - limit length and remove invalid characters
        safe_name = obj.name[:15].replace(' ', '_').replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
        filename = f"{prefix}_{obj.id}_{safe_name}.png"
        filepath = os.path.join(output_dir, filename)
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        img.save(filepath)
        
        # Store barcode data in object (if we add a barcode field later)
        self.stdout.write(f'Generated QR code: {filename} -> {data}')

    def _generate_code128(self, data, obj, output_dir, size, prefix='prod'):
        """Generate Code128 barcode for the given data and object."""
        try:
            import barcode
            from barcode.writer import ImageWriter
            
            # Create Code128 barcode
            code = barcode.get_barcode_class('code128')
            barcode_instance = code(data, writer=ImageWriter())
            
            # Create safe filename
            safe_name = obj.name[:15].replace(' ', '_').replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
            filename = f"{prefix}_{obj.id}_{safe_name}.png"
            filepath = os.path.join(output_dir, filename)
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            barcode_instance.save(filepath)
            
            self.stdout.write(f'Generated Code128: {filename} -> {data}')
            
        except ImportError:
            self.stdout.write(
                self.style.ERROR(
                    'Code128 generation requires python-barcode package. Install with: pip install python-barcode'
                )
            )
            # Fallback to QR code
            self._generate_qr_code(data, obj, output_dir, size, prefix)

