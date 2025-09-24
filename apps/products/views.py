from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET
from django.db.models import Q
from .models import Product

def index(request):
    return HttpResponse("Products app OK")

@require_GET
def api_search(request):
    """Search products by name, brand, or SKU with optional type filtering."""
    query = request.GET.get('q', '').strip()
    product_type = request.GET.get('type', '').strip()
    
    if not query:
        return JsonResponse({'products': []})
    
    products = Product.objects.filter(
        Q(name__icontains=query) | 
        Q(brand__name__icontains=query) | 
        Q(sku__icontains=query),
        is_active=True
    )
    
    # Filter by product type if specified
    if product_type in ['moto', 'piece']:
        products = products.filter(product_type=product_type)
    
    products = products.select_related('brand').order_by('name')[:20]
    
    return JsonResponse({
        'products': [
            {
                'id': product.id,
                'name': product.name,
                'brand': product.brand.name if product.brand else None,
                'sku': product.sku,
                'cost_price': float(product.cost_price or 0),
                'wholesale_price': float(product.wholesale_price or 0),
                'selling_price': float(product.selling_price or 0),
                'product_type': product.product_type
            }
            for product in products
        ]
    })