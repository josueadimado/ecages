from django.db import models
from django.utils import timezone
from apps.products.models import Product
from apps.providers.models import Brand  # <-- new
from django.conf import settings
from django.db import transaction
from django.db.models import F, Case, When, IntegerField
from django.db.models.signals import post_save
from django.dispatch import receiver

class SalesPoint(models.Model):
    name = models.CharField(max_length=150, unique=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)                     # <-- new
    brand = models.ForeignKey(Brand, null=True, blank=True,                 # <-- new
                              on_delete=models.PROTECT, related_name="salespoints")
    # Flag to designate the warehouse entity
    is_warehouse = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

class Stock(models.Model):
    salespoint = models.ForeignKey(SalesPoint, on_delete=models.CASCADE, related_name="stocks")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stocks")
    opening_qty = models.PositiveIntegerField(default=0)
    quantity = models.IntegerField(default=0)

    class Meta:
        unique_together = ("salespoint", "product")

    def __str__(self):
        return f"{self.salespoint} - {self.product} ({self.quantity})"

class Transfer(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    from_salespoint = models.ForeignKey(SalesPoint, on_delete=models.SET_NULL, null=True, related_name="transfers_out")
    to_salespoint = models.ForeignKey(SalesPoint, on_delete=models.SET_NULL, null=True, related_name="transfers_in")
    quantity = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    # Manager acknowledgement at destination (optional validation step)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='transfers_acknowledged')

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Transfert {self.product} {self.quantity} de {self.from_salespoint} à {self.to_salespoint}"


class SalesPointStock(models.Model):
    salespoint = models.ForeignKey(
        SalesPoint,
        on_delete=models.CASCADE,
        related_name="salespoint_stocks",  # <-- was "stocks", must be unique
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="salespoint_stocks",
    )
    opening_qty   = models.IntegerField(default=0)
    sold_qty      = models.IntegerField(default=0)
    transfer_in   = models.IntegerField(default=0)
    transfer_out  = models.IntegerField(default=0)
    alert_qty     = models.IntegerField(default=5)
    # Quantity temporarily held by pending sales (not yet validated by cashier)
    reserved_qty  = models.IntegerField(default=0)

    class Meta:
        unique_together = ("salespoint", "product")
        ordering = ["product__name"]
        indexes = [
            models.Index(fields=["salespoint", "product"]),
            models.Index(fields=["salespoint", "alert_qty"]),  # For stock alerts
            models.Index(fields=["product", "opening_qty"]),   # For product availability
        ]

    def __str__(self):
        return f"{self.salespoint} • {self.product}"

    @property
    def remaining_qty(self):
        rem = (self.opening_qty + self.transfer_in) - (self.sold_qty + self.transfer_out)
        return rem if rem > 0 else 0

    @property
    def available_qty(self):
        """
        Quantity that can still be sold right now (remaining minus reserved).
        Never returns negative values.
        """
        try:
            rem = int(self.remaining_qty)
        except Exception:
            rem = 0
        try:
            res = int(self.reserved_qty or 0)
        except Exception:
            res = 0
        val = rem - res
        return val if val > 0 else 0

    # ==== Atomic stock flows: reserve -> (commit|release) ====
    @classmethod
    def reserve_stock(cls, salespoint, product, qty: int):
        """
        Atomically reserve `qty` units so they cannot be sold by another draft.
        Raises ValueError if insufficient available quantity.
        """
        if qty <= 0:
            raise ValueError("Quantity must be positive")
        with transaction.atomic():
            sps = (
                cls.objects.select_for_update()
                .get(salespoint=salespoint, product=product)
            )
            if sps.available_qty < qty:
                raise ValueError("Insufficient stock to reserve")
            # atomic increment
            cls.objects.filter(pk=sps.pk).update(reserved_qty=F("reserved_qty") + qty)
            sps.refresh_from_db(fields=["reserved_qty"])  # reflect new value
            return sps

    @classmethod
    def release_stock(cls, salespoint, product, qty: int):
        """
        Atomically release a previous reservation (e.g., on cancel/reject).
        If qty is greater than current reserved, reserved becomes 0.
        """
        if qty <= 0:
            return
        with transaction.atomic():
            sps = (
                cls.objects.select_for_update()
                .get(salespoint=salespoint, product=product)
            )
            new_val = max(0, int(sps.reserved_qty or 0) - int(qty))
            cls.objects.filter(pk=sps.pk).update(reserved_qty=new_val)
            sps.refresh_from_db(fields=["reserved_qty"])  # reflect new value
            return sps

    @classmethod
    def commit_stock(cls, salespoint, product, qty: int):
        """
        Convert a reservation into a real sale: reserved -= qty, sold += qty.
        Raises ValueError if there isn't enough reserved to commit.
        """
        if qty <= 0:
            raise ValueError("Quantity must be positive")
        with transaction.atomic():
            sps = (
                cls.objects.select_for_update()
                .get(salespoint=salespoint, product=product)
            )
            if (sps.reserved_qty or 0) < qty:
                raise ValueError("Insufficient reserved stock to commit")
            # atomic update
            cls.objects.filter(pk=sps.pk).update(
                reserved_qty=F("reserved_qty") - qty,
                sold_qty=F("sold_qty") + qty,
            )
            sps.refresh_from_db(fields=["reserved_qty", "sold_qty"])  # reflect new values
            return sps

    @staticmethod
    def _log_txn(*, salespoint, product, qty: int, reason: str, reference: str = "", user=None):
        """Create a StockTransaction row (safe helper)."""
        try:
            StockTransaction.objects.create(
                salespoint=salespoint,
                product=product,
                qty=int(qty or 0),
                reason=reason,
                reference=reference or "",
                user=user,
            )
        except Exception:
            # Do not break core stock flow if audit write fails
            pass

    # ==== Batch helpers that operate on a Sale and its items ====
    @classmethod
    def reserve_for_sale(cls, sale):
        """Reserve stock for every item in the given draft sale (awaiting cashier)."""
        if not getattr(sale, "salespoint_id", None):
            return
        for it in sale.items.select_related("product"):
            qty = int(getattr(it, "quantity", 0) or 0)
            if qty > 0:
                cls.reserve_stock(sale.salespoint, it.product, qty)

    @classmethod
    def release_for_sale(cls, sale):
        """Release previously reserved stock for every item in a canceled/rejected sale."""
        if not getattr(sale, "salespoint_id", None):
            return
        for it in sale.items.select_related("product"):
            qty = int(getattr(it, "quantity", 0) or 0)
            if qty > 0:
                cls.release_stock(sale.salespoint, it.product, qty)

    @classmethod
    def commit_for_sale(cls, sale):
        """Commit reserved stock for every item in an approved sale (moves reserved -> sold)."""
        if not getattr(sale, "salespoint_id", None):
            return
        for it in sale.items.select_related("product"):
            qty = int(getattr(it, "quantity", 0) or 0)
            if qty > 0:
                cls.commit_stock(sale.salespoint, it.product, qty)
                # Log a negative movement for approval
                ref = getattr(sale, "number", None) or str(getattr(sale, "id", ""))
                cashier = getattr(sale, "cashier", None)
                cls._log_txn(salespoint=sale.salespoint, product=it.product, qty=-qty,
                             reason="sale", reference=ref, user=cashier)

    def can_sell(self, qty: int) -> bool:
        """Quick helper for UI validations."""
        try:
            return int(qty) > 0 and self.available_qty >= int(qty)
        except Exception:
            return False

class StockTransaction(models.Model):
    """
    Immutable audit log of stock movements at a salespoint.
    Positive qty = stock increases, Negative qty = stock decreases.
    This model is designed to be immutable - no updates or deletes allowed.
    """
    REASON_CHOICES = (
        ("sale", "Vente"),
        ("return", "Retour client"),
        ("transfer_in", "Transfert entrant"),
        ("transfer_out", "Transfert sortant"),
        ("restock", "Réapprovisionnement"),
        ("adjustment", "Ajustement"),
        ("restock_sent", "Réapprovisionnement envoyé"),
        ("restock_received", "Réapprovisionnement reçu"),
        ("grn", "Bon de réception"),
        ("dn", "Bon de livraison"),
        ("cycle_count", "Inventaire cyclique"),
        ("write_off", "Mise au rebut"),
    )

    salespoint = models.ForeignKey('SalesPoint', on_delete=models.CASCADE, related_name='stock_transactions')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE, related_name='stock_transactions')
    qty = models.IntegerField(help_text="Quantité (positive = entrée, négative = sortie)")
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    reference = models.CharField(max_length=64, blank=True, default="", help_text="Référence externe (numéro de vente, transfert, etc.)")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_transactions')
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Additional audit fields for better tracking
    document_type = models.CharField(max_length=50, blank=True, default="", help_text="Type de document (Sale, RestockRequest, etc.)")
    document_id = models.PositiveIntegerField(null=True, blank=True, help_text="ID du document source")
    notes = models.TextField(blank=True, default="", help_text="Notes additionnelles")
    
    # GPS and photo fields for proof of delivery/receipt
    gps_latitude = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True, help_text="Latitude GPS")
    gps_longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True, help_text="Longitude GPS")
    photo_url = models.URLField(blank=True, default="", help_text="URL de la photo de preuve")
    
    # Reversal tracking
    is_reversal = models.BooleanField(default=False, help_text="Indique si c'est une transaction d'annulation")
    reversed_transaction = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='reversals', help_text="Transaction annulée par celle-ci")
    reversal_reason = models.CharField(max_length=255, blank=True, default="", help_text="Raison de l'annulation")

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["salespoint", "product", "created_at"]),
            models.Index(fields=["reason", "created_at"]),
            models.Index(fields=["reference", "created_at"]),  # For reference lookups
            models.Index(fields=["salespoint", "reason", "created_at"]),  # For salespoint reports
        ]
        verbose_name = "Mouvement de stock"
        verbose_name_plural = "Mouvements de stock"

    def __str__(self):
        sign = "+" if self.qty >= 0 else "-"
        return f"[{self.get_reason_display()}] {self.salespoint} • {self.product} • {sign}{abs(self.qty)}"
    
    def save(self, *args, **kwargs):
        """Prevent updates to existing transactions - only allow creation."""
        if self.pk:
            raise ValueError("StockTransaction is immutable - cannot update existing records")
        super().save(*args, **kwargs)
    
    def delete(self, *args, **kwargs):
        """Prevent deletion of transactions - use reversal instead."""
        raise ValueError("StockTransaction is immutable - cannot delete. Use create_reversal() instead")
    
    def create_reversal(self, user, reason="", notes=""):
        """Create a reversal transaction that cancels this one."""
        if self.is_reversal:
            raise ValueError("Cannot reverse a reversal transaction")
        
        return StockTransaction.objects.create(
            salespoint=self.salespoint,
            product=self.product,
            qty=-self.qty,  # Opposite quantity
            reason=self.reason,  # Same reason
            reference=f"REV-{self.reference}" if self.reference else "",
            user=user,
            document_type=self.document_type,
            document_id=self.document_id,
            notes=f"Annulation: {notes}" if notes else "Transaction annulée",
            is_reversal=True,
            reversed_transaction=self,
            reversal_reason=reason,
        )
    
    @classmethod
    def create_transaction(cls, salespoint, product, qty, reason, reference="", user=None, 
                          document_type="", document_id=None, notes="", gps_lat=None, gps_lng=None, photo_url=""):
        """Helper method to create a transaction with all audit fields."""
        return cls.objects.create(
            salespoint=salespoint,
            product=product,
            qty=qty,
            reason=reason,
            reference=reference,
            user=user,
            document_type=document_type,
            document_id=document_id,
            notes=notes,
            gps_latitude=gps_lat,
            gps_longitude=gps_lng,
            photo_url=photo_url,
        )

@receiver(post_save, sender=Transfer)
def _txn_on_transfer(sender, instance: Transfer, created, **kwargs):
    """Create movement logs when a transfer is acknowledged.
    - Inter-salespoint: reasons transfer_out / transfer_in
    - Warehouse -> Salespoint restock: reasons restock (negative at warehouse, positive at destination)
    """
    # Only log when reception is acknowledged
    if not getattr(instance, "acknowledged_at", None):
        return
    try:
        src = getattr(instance, "from_salespoint", None)
        dst = getattr(instance, "to_salespoint", None)
        is_wh = bool(getattr(src, "is_warehouse", False))
        out_reason = "restock" if is_wh else "transfer_out"
        in_reason = "restock" if is_wh else "transfer_in"
        if src:
            StockTransaction.objects.create(
                salespoint=src,
                product=instance.product,
                qty=-int(instance.quantity or 0),
                reason=out_reason,
                reference=f"T{instance.id}",
                user=None,
            )
        if dst:
            StockTransaction.objects.create(
                salespoint=dst,
                product=instance.product,
                qty=int(instance.quantity or 0),
                reason=in_reason,
                reference=f"T{instance.id}",
                user=None,
            )
    except Exception:
        # Never block transfer ack on audit trail issues
        pass


# === Inter-salespoint Transfer Request (Manager to Manager) ===
class TransferRequest(models.Model):
    STATUS = (
        ("draft", "Brouillon"),
        ("sent", "Envoyée"),
        ("approved", "Approuvée"),
        ("rejected", "Rejetée"),
        ("fulfilled", "Servie"),
        ("cancelled", "Annulée"),
    )

    from_salespoint = models.ForeignKey(SalesPoint, on_delete=models.CASCADE, related_name="transfer_requests_out")
    to_salespoint = models.ForeignKey(SalesPoint, on_delete=models.CASCADE, related_name="transfer_requests_in")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="transfer_requests")
    status = models.CharField(max_length=12, choices=STATUS, default="draft")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="transfer_requests_approved")
    # Per-salespoint daily running number for sent requests
    number = models.CharField(max_length=40, blank=True, default="", unique=True, db_index=True)
    number_date = models.DateField(null=True, blank=True)
    number_seq = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["from_salespoint", "to_salespoint", "status", "created_at"]),
            models.Index(fields=["from_salespoint", "number_date", "number_seq"]),
        ]

    def __str__(self):
        return f"TR#{self.pk} {self.from_salespoint} → {self.to_salespoint} ({self.status})"


class TransferRequestLine(models.Model):
    request = models.ForeignKey(TransferRequest, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    # Snapshot numbers (optional)
    available_at_source = models.IntegerField(default=0)

    class Meta:
        unique_together = ("request", "product")
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name="transfer_req_qty_gt_0"),
        ]

    def __str__(self):
        return f"{self.product} x {self.quantity} (TR {self.request_id})"


# === Restock Request (SalesPoint -> Warehouse) ===
class RestockRequest(models.Model):
    STATUS = (
        ("draft", "Brouillon"),
        ("sent", "Envoyée"),
        ("approved", "Approuvée"),
        ("rejected", "Rejetée"),
        ("fulfilled", "Servie"),
        ("cancelled", "Annulée"),
        ("partially_validated", "Partiellement validé"),
        ("validated", "Validé"),
    )

    salespoint = models.ForeignKey(SalesPoint, on_delete=models.CASCADE, related_name="restock_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="restock_requests")
    provider = models.ForeignKey('providers.Provider', on_delete=models.PROTECT, null=True, blank=True, related_name="restock_requests")
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    notes = models.TextField(blank=True)
    reference = models.CharField(max_length=50, blank=True, help_text="Reference number (e.g., WH-DDMMYY-P-0001)")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Total purchase amount")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["salespoint", "status", "created_at"]),
            models.Index(fields=["status", "created_at"]),  # For status filtering
            models.Index(fields=["reference", "created_at"]),  # For reference lookups
        ]

    def __str__(self):
        return f"Restock #{self.pk} • {self.salespoint} • {self.status}"
    
    @property
    def total_quantity(self):
        """Calculate total quantity across all lines in this request."""
        total = 0
        for line in self.lines.all():
            # Use quantity_approved first, fallback to quantity_requested, then quantity
            qty = line.quantity_approved or line.quantity_requested or line.quantity or 0
            total += qty
        return total


class RestockLine(models.Model):
    request = models.ForeignKey(RestockRequest, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_requested = models.PositiveIntegerField(null=True, blank=True, help_text="Quantity originally requested")
    quantity_approved = models.PositiveIntegerField(null=True, blank=True, help_text="Quantity approved by warehouse")
    validated_at = models.DateTimeField(null=True, blank=True, help_text="When this line was validated by salespoint")

    # Optional snapshot fields for audit
    remaining_qty = models.IntegerField(default=0)
    alert_qty = models.IntegerField(default=0)
    
    # Stock quantities at validation time
    stock_qty_at_validation = models.IntegerField(null=True, blank=True, help_text="Salespoint stock quantity at validation time")
    
    # Legacy field for compatibility
    quantity = models.PositiveIntegerField(default=0, help_text="Legacy field - use quantity_requested instead")

    class Meta:
        unique_together = ("request", "product")
        indexes = [
            models.Index(fields=["request", "product"]),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(quantity_requested__gt=0), name="restock_qty_requested_gt_0"),
        ]

    @property
    def effective_quantity(self):
        """Get the effective quantity (approved > requested > legacy quantity)."""
        return self.quantity_approved or self.quantity_requested or self.quantity or 0
    
    def __str__(self):
        return f"{self.product} x {self.effective_quantity} (req {self.request_id})"


class RestockValidationAudit(models.Model):
    """Audit trail for restock validations - captures stock state at validation time."""
    restock_request = models.ForeignKey(RestockRequest, on_delete=models.CASCADE, related_name="validation_audits")
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="restock_validations")
    validated_at = models.DateTimeField(auto_now_add=True)
    
    # Stock quantities at validation time
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_validated = models.PositiveIntegerField(help_text="Quantity validated by manager")
    stock_before_validation = models.IntegerField(help_text="Stock quantity before validation")
    stock_after_validation = models.IntegerField(help_text="Stock quantity after validation")
    
    # Additional context
    cost_price_at_validation = models.DecimalField(max_digits=12, decimal_places=2, help_text="Product cost price at validation time")
    total_value = models.DecimalField(max_digits=12, decimal_places=2, help_text="Total value of validated quantity")
    
    class Meta:
        ordering = ["-validated_at"]
        indexes = [
            models.Index(fields=["restock_request", "validated_at"]),
            models.Index(fields=["product", "validated_at"]),
        ]
        verbose_name = "Audit de validation d'approvisionnement"
        verbose_name_plural = "Audits de validation d'approvisionnement"
    
    def __str__(self):
        return f"Validation {self.product} x {self.quantity_validated} (REQ{self.restock_request.id})"


class RestockRequestItem(models.Model):
    """Individual items in a restock request from commercial director to warehouse."""
    request = models.ForeignKey(RestockRequest, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(help_text="Quantity to restock")
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Purchase cost price")
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Wholesale price")
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Selling price")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Total cost for this item")
    
    # Validation fields
    quantity_validated = models.PositiveIntegerField(null=True, blank=True, help_text="Quantity validated by warehouse manager")
    validated_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="restock_item_validations")
    
    class Meta:
        unique_together = ("request", "product")
        ordering = ["product__name"]
        indexes = [
            models.Index(fields=["request", "product"]),
            models.Index(fields=["validated_at"]),
        ]
    
    def save(self, *args, **kwargs):
        # Auto-calculate total cost
        self.total_cost = self.quantity * self.cost_price
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.product.name} x {self.quantity} (REQ{self.request.id})"


# === Warehouse -> Commercial Director purchase request (Commande) ===
class WarehousePurchaseRequest(models.Model):
    STATUS = (
        ("draft", "Brouillon"),
        ("sent", "Envoyée"),
        ("acknowledged", "Accusée"),
        ("cancelled", "Annulée"),
    )

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="warehouse_purchase_requests")
    status = models.CharField(max_length=20, choices=STATUS, default="sent")
    reference = models.CharField(max_length=50, blank=True, help_text="Reference number (e.g., CMD-WH-DDMMYY-0001)")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["reference", "created_at"]),
        ]

    def __str__(self):
        return f"WH-CMD {self.reference or self.pk} ({self.status})"


class WarehousePurchaseLine(models.Model):
    request = models.ForeignKey(WarehousePurchaseRequest, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_requested = models.PositiveIntegerField()

    class Meta:
        unique_together = ("request", "product")
        constraints = [
            models.CheckConstraint(check=models.Q(quantity_requested__gt=0), name="wh_cmd_qty_gt_0"),
        ]

    def __str__(self):
        return f"{self.product} x {self.quantity_requested} (CMD {self.request_id})"


# === Goods Received Note (GRN) - Warehouse inbound confirmation ===
class GoodsReceivedNote(models.Model):
    """Goods Received Note - confirms receipt of goods from providers at warehouse."""
    STATUS = (
        ("draft", "Brouillon"),
        ("confirmed", "Confirmé"),
        ("cancelled", "Annulé"),
    )
    
    reference = models.CharField(max_length=50, unique=True, help_text="Référence GRN (ex: GRN-WH-DDMMYY-0001)")
    provider = models.ForeignKey('providers.Provider', on_delete=models.PROTECT, related_name="grns")
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="grns_received")
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    
    # Proof of delivery
    gps_latitude = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True)
    gps_longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    photo_url = models.URLField(blank=True, default="")
    signature_url = models.URLField(blank=True, default="")
    
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["reference"]),
            models.Index(fields=["status", "created_at"]),
        ]
    
    def __str__(self):
        return f"GRN {self.reference} - {self.provider} ({self.status})"


class GoodsReceivedLine(models.Model):
    """Line item for GRN - what was actually received."""
    grn = models.ForeignKey(GoodsReceivedNote, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_received = models.PositiveIntegerField(help_text="Quantité réellement reçue")
    quantity_ordered = models.PositiveIntegerField(default=0, help_text="Quantité commandée (pour référence)")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Coût unitaire")
    notes = models.TextField(blank=True, default="")
    
    class Meta:
        unique_together = ("grn", "product")
        constraints = [
            models.CheckConstraint(check=models.Q(quantity_received__gt=0), name="grn_line_qty_gt_0"),
        ]
    
    def __str__(self):
        return f"{self.product} x {self.quantity_received} (GRN {self.grn_id})"


# === Delivery Note (DN) - Warehouse outbound confirmation ===
class DeliveryNote(models.Model):
    """Delivery Note - confirms dispatch of goods from warehouse to salespoints."""
    STATUS = (
        ("draft", "Brouillon"),
        ("dispatched", "Expédié"),
        ("delivered", "Livré"),
        ("cancelled", "Annulé"),
    )
    
    reference = models.CharField(max_length=50, unique=True, help_text="Référence DN (ex: DN-WH-DDMMYY-0001)")
    to_salespoint = models.ForeignKey(SalesPoint, on_delete=models.PROTECT, related_name="delivery_notes")
    dispatched_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dns_dispatched")
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, 
                                   related_name="dns_received")
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    
    # Proof of delivery
    dispatch_gps_lat = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True)
    dispatch_gps_lng = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    delivery_gps_lat = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True)
    delivery_gps_lng = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    photo_url = models.URLField(blank=True, default="")
    signature_url = models.URLField(blank=True, default="")
    
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["reference"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["to_salespoint", "status"]),
        ]
    
    def __str__(self):
        return f"DN {self.reference} - {self.to_salespoint} ({self.status})"


class DeliveryLine(models.Model):
    """Line item for DN - what was actually dispatched."""
    dn = models.ForeignKey(DeliveryNote, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_dispatched = models.PositiveIntegerField(help_text="Quantité expédiée")
    quantity_received = models.PositiveIntegerField(default=0, help_text="Quantité reçue (confirmée par le destinataire)")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Coût unitaire")
    notes = models.TextField(blank=True, default="")
    
    class Meta:
        unique_together = ("dn", "product")
        constraints = [
            models.CheckConstraint(check=models.Q(quantity_dispatched__gt=0), name="dn_line_qty_gt_0"),
        ]
    
    def __str__(self):
        return f"{self.product} x {self.quantity_dispatched} (DN {self.dn_id})"


# === Cycle Count - Daily inventory checks ===
class CycleCount(models.Model):
    """Daily cycle count for inventory verification."""
    STATUS = (
        ("draft", "Brouillon"),
        ("in_progress", "En cours"),
        ("completed", "Terminé"),
        ("approved", "Approuvé"),
    )
    
    salespoint = models.ForeignKey(SalesPoint, on_delete=models.CASCADE, related_name="cycle_counts")
    counted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cycle_counts")
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    count_date = models.DateField(help_text="Date de l'inventaire")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, 
                                   related_name="cycle_counts_approved")
    
    class Meta:
        ordering = ["-count_date", "-created_at"]
        indexes = [
            models.Index(fields=["salespoint", "count_date"]),
            models.Index(fields=["status", "count_date"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["salespoint", "count_date"], name="unique_cycle_count_per_sp_date"),
        ]
    
    def __str__(self):
        return f"Inventaire {self.salespoint} - {self.count_date} ({self.status})"


class CycleCountLine(models.Model):
    """Line item for cycle count - actual vs expected quantities."""
    cycle_count = models.ForeignKey(CycleCount, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    expected_qty = models.IntegerField(help_text="Quantité attendue (selon le système)")
    actual_qty = models.IntegerField(help_text="Quantité réelle comptée")
    variance = models.IntegerField(help_text="Écart (actual - expected)")
    notes = models.TextField(blank=True, default="")
    
    class Meta:
        unique_together = ("cycle_count", "product")
    
    def save(self, *args, **kwargs):
        self.variance = self.actual_qty - self.expected_qty
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.product} - Attendu: {self.expected_qty}, Réel: {self.actual_qty} (Écart: {self.variance:+d})"