from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from apps.products.models import Product
from apps.inventory.models import SalesPoint


class Sale(models.Model):
    KIND = (("P", "Pièces"), ("M", "Moto"))
    PAYMENTS = (("cash", "Espèce"), ("mobile", "Mobile Money"), ("card", "Carte"))
    STATUS = (
        ("draft", "Brouillon"),
        ("awaiting_cashier", "En attente de caisse"),
        ("approved", "Validée par caisse"),
        ("rejected", "Rejetée"),
        ("cancelled", "Annulée"),
    )

    salespoint = models.ForeignKey(SalesPoint, on_delete=models.CASCADE, related_name="sales")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales_made")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=1, choices=KIND, default="P")
    number = models.CharField(max_length=32, db_index=True)  # e.g. AD-140825-P-0001
    customer_name = models.CharField(max_length=120, default="DIVERS", blank=True)
    customer_phone = models.CharField(max_length=40, blank=True, default="")
    payment_type = models.CharField(max_length=10, choices=PAYMENTS, default="cash")
    status = models.CharField(max_length=20, choices=STATUS, default="awaiting_cashier")

    # Totals (selling side)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Cost & profit (NEW)
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), help_text="Somme des coûts des lignes au moment de la vente")
    gross_profit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), help_text="Marge brute = total_amount - total_cost")

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales_validated",
        help_text="Caissier/ère ayant validé la vente",
    )
    approved_at = models.DateTimeField(null=True, blank=True, help_text="Horodatage de validation par la caisse")
    cancelled_at = models.DateTimeField(null=True, blank=True, help_text="Horodatage d'annulation")
    received_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Montant reçu du client lors de la validation",
    )

    # Extended moto/customer details
    customer_details = models.JSONField(
        blank=True,
        null=True,
        default=dict,
        help_text="Détails client étendus (nom, téléphones, société, domicile, profession, adresse, demeurant à, etc.)",
    )
    chassis_number = models.CharField(max_length=100, blank=True, default="", help_text="N° de châssis (moto)")
    engine_number = models.CharField(max_length=100, blank=True, default="", help_text="N° de moteur (moto)")
    amount_in_words = models.CharField(max_length=255, blank=True, default="", help_text="Montant en lettres")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["salespoint", "number"], name="unique_sale_number_per_sp"),
            models.CheckConstraint(check=models.Q(total_amount__gte=0), name="sale_total_amount_gte_0"),
            models.CheckConstraint(
                check=models.Q(received_amount__gte=0) | models.Q(received_amount__isnull=True),
                name="sale_received_amount_none_or_gte_0",
            ),
            models.CheckConstraint(check=models.Q(total_cost__gte=0), name="sale_total_cost_gte_0"),
            models.CheckConstraint(check=models.Q(gross_profit__gte=0) | models.Q(gross_profit__lte=0), name="sale_gross_profit_any"),
        ]
        indexes = [
            models.Index(fields=["salespoint", "created_at"], name="sale_sp_created_idx"),
            models.Index(fields=["status"], name="sale_status_idx"),
            models.Index(fields=["salespoint", "status"], name="sale_sp_status_idx"),
        ]

    def __str__(self):
        return f"{self.number} • {self.salespoint}"

    def recalc_total(self):
        """Recalcule `total_amount`, `total_cost`, `gross_profit` à partir des lignes liées.
        Ne sauvegarde pas l'instance par défaut.
        """
        total = Decimal("0.00")
        cost = Decimal("0.00")
        for it in self.items.all():
            total += it.line_total or Decimal("0.00")
            cost += it.line_cost or Decimal("0.00")
        self.total_amount = total
        self.total_cost = cost
        self.gross_profit = (total - cost)
        return total

    def approve(self, cashier, received_amount: Decimal | int | float | None = None, save: bool = True):
        """Marque la vente comme validée par la caisse."""
        if self.status not in ("awaiting_cashier", "draft"):
            return self
        self.status = "approved"
        self.cashier = cashier
        if received_amount is not None:
            self.received_amount = Decimal(received_amount)
        self.approved_at = timezone.now()
        if save:
            self.save(update_fields=["status", "cashier", "received_amount", "approved_at"])
        return self

    def mark_cancelled(self, save: bool = True):
        """Marque la vente comme annulée (sans gérer la logistique)."""
        if self.status == "approved":
            return self
        self.status = "cancelled"
        self.cancelled_at = timezone.now()
        if save:
            self.save(update_fields=["status", "cancelled_at"])
        return self

    @property
    def change_due(self) -> Decimal:
        """Rendu à donner au client (si `received_amount` est défini)."""
        if self.received_amount is None:
            return Decimal("0.00")
        return (self.received_amount or Decimal("0.00")) - (self.total_amount or Decimal("0.00"))

    @property
    def can_print_receipt(self) -> bool:
        return self.status == "approved"

    @property
    def is_awaiting_cashier(self) -> bool:
        return self.status == "awaiting_cashier"


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    # Selling values
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    # Cost & profit (NEW)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), help_text="Coût unitaire au moment de la vente")
    line_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    line_profit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        indexes = [
            models.Index(fields=["sale", "product"], name="saleitem_sale_prod_idx"),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name="saleitem_qty_gt_0"),
            models.CheckConstraint(check=models.Q(unit_price__gte=0), name="saleitem_price_gte_0"),
            models.CheckConstraint(check=models.Q(unit_cost__gte=0), name="saleitem_cost_gte_0"),
        ]

    def clean(self):
        """Business validation for line items.
        - For moto sales (sale.kind == 'M'): enforce a single line and quantity == 1.
        - Prevent adding multiple items to a moto sale.
        """
        # If no sale yet (e.g., inline form init), skip.
        if not self.sale_id and not getattr(self, "sale", None):
            return

        # Ensure we have the related sale loaded (avoid extra query if already prefetched)
        sale = self.sale
        if sale is None and self.sale_id:
            # Defensive: fetch minimally when needed
            sale = type(self).objects.select_related("sale").only("sale__id").get(pk=self.pk).sale  # pragma: no cover

        if sale and getattr(sale, "kind", None) == "M":
            # Enforce quantity == 1
            if (self.quantity or 0) != 1:
                raise ValidationError({
                    "quantity": "Une vente de moto doit avoir une quantité égale à 1.",
                })

            # Enforce single line for a moto sale
            qs = SaleItem.objects.filter(sale=sale)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("Une vente de moto ne peut contenir qu'une seule ligne.")

    def save(self, *args, **kwargs):
        # Validate business rules before computing totals/persisting
        self.full_clean()

        # Compute selling total
        qty = self.quantity or 0
        self.line_total = (self.unit_price or Decimal("0.00")) * qty

        # Capture cost from product at time of sale if not explicitly provided
        if self.unit_cost in (None, Decimal("0.00")) and self.product_id:
            try:
                # Product is expected to expose `cost_price` (Decimal)
                self.unit_cost = getattr(self.product, "cost_price", Decimal("0.00")) or Decimal("0.00")
            except Exception:
                self.unit_cost = Decimal("0.00")

        self.line_cost = (self.unit_cost or Decimal("0.00")) * qty
        self.line_profit = (self.line_total or Decimal("0.00")) - (self.line_cost or Decimal("0.00"))

        super().save(*args, **kwargs)

        # After saving a line, keep the parent sale totals in sync
        if self.sale_id:
            self.sale.recalc_total()
            # Avoid recursion; only update the 3 total fields
            Sale.objects.filter(pk=self.sale_id).update(
                total_amount=self.sale.total_amount,
                total_cost=self.sale.total_cost,
                gross_profit=self.sale.gross_profit,
            )

    def __str__(self):
        return f"{self.product} x {self.quantity}"


class CancellationRequest(models.Model):
    STATUS = [("pending", "En attente"), ("approved", "Approuvée"), ("rejected", "Rejetée")]
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="cancellations")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Annulation {self.sale.number} - {self.status}"


# --- NEW: CancellationLine model ---
class CancellationLine(models.Model):
    """Snapshot of items requested for cancellation (supports partial cancellations)."""
    request = models.ForeignKey(CancellationRequest, on_delete=models.CASCADE, related_name="lines")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    # Snapshot values for audit
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    unit_cost  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name="cxl_line_qty_gt_0"),
        ]

    def __str__(self):
        return f"CxlLine req={self.request_id} item={self.sale_item_id} qty={self.quantity}"


# --- NEW: Notifications for managers/users ---
class Notification(models.Model):
    """Simple user notification model.
    Stores message text, optional link, and read timestamp.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=255, blank=True, default="")
    kind = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["user", "read_at"]),
        ]

    def mark_read(self):
        if not self.read_at:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])

    def __str__(self):
        return f"Notif({self.user_id}) {self.message[:40]}"