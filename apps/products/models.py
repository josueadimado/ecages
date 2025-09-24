# apps/products/models.py
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from apps.providers.models import Provider, Brand

class Product(models.Model):
    TYPE_CHOICES = [
        ("piece", "Pièce"),
        ("moto", "Moto"),
    ]

    # Ownership (FINAL: required)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="products")
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, related_name="products")

    # Identity
    name = models.CharField(max_length=255)
    model = models.CharField(max_length=120, blank=True)
    sku = models.CharField(max_length=64, blank=True, null=True, unique=False, db_index=True)

    # Pricing
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Type & rules
    product_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default="piece")
    min_quantity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # Timestamps
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["product_type"]),
            models.Index(fields=["provider", "brand"]),
            models.Index(fields=["name"]),
        ]

    def clean(self):
        # Keep the validation (works in admin/forms)
        if self.brand_id and self.provider_id and self.brand.provider_id != self.provider_id:
            raise ValidationError("La marque sélectionnée n'appartient pas au fournisseur choisi.")
        for field in ("cost_price", "selling_price", "wholesale_price", "discount_price"):
            value = getattr(self, field) or Decimal("0.00")
            if value < 0:
                raise ValidationError({field: "Le prix ne peut pas être négatif."})

    def __str__(self) -> str:
        return f"{self.name} [{self.sku}]"