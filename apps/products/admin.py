from django.contrib import admin
from .models import Product

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name", "sku", "product_type", "provider", "brand",
        "selling_price", "wholesale_price", "discount_price", "cost_price",
        "min_quantity", "is_active",
    )
    list_filter = ("product_type", "provider", "brand", "is_active")
    search_fields = ("name", "sku", "brand__name", "provider__name")
    autocomplete_fields = ("provider", "brand")
    readonly_fields = ("created_at", "updated_at")
    
    # Performance optimizations
    list_per_page = 50  # Reduce from default 100
    list_select_related = ("provider", "brand")
    search_help_text = "Recherche par nom, SKU, marque ou fournisseur"
    
    # Add date hierarchy for better navigation
    date_hierarchy = "created_at" if hasattr(Product, "created_at") else None
    fieldsets = (
        (None, {
            "fields": (("provider",), ("name", "model"), ("brand",), ("sku", "product_type"))
        }),
        ("Tarification", {
            "fields": (("cost_price", "selling_price"), ("wholesale_price", "discount_price"))
        }),
        ("Règles & Suivi", {
            "fields": (("min_quantity", "is_active"), ("created_at", "updated_at"))
        }),
    )