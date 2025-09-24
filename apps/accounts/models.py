from django.contrib.auth.models import AbstractUser
from django.db import models
from apps.inventory.models import SalesPoint


class User(AbstractUser):
    ROLE_CHOICES = [
        ("sales", "Vendeur"),
        ("sales_manager", "Responsable Point de Vente"),
        ("cashier", "Caissier"),
        ("commercial_dir", "Directeur Commercial"),
        ("warehouse_mgr", "Gestionnaire Entrepôt"),
        ("stock_mgr", "Gestionnaire de Stock"),
        ("hr", "RH"),
        ("accountant", "Comptable"),
        ("secretary", "Secrétaire"),
        ("ceo", "CEO"),
        ("admin", "Admin"),
    ]
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default="sales")
    salespoint = models.ForeignKey(
        SalesPoint,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="users"
    )

    def __str__(self):
        full = self.get_full_name() or self.username
        return f"{full} ({self.get_role_display()})"