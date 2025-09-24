# apps/inventory/admin.py
from django.contrib import admin
from .models import (
    SalesPoint, Stock, Transfer, SalesPointStock, StockTransaction,
    TransferRequest, TransferRequestLine, RestockRequest, RestockRequestItem, RestockValidationAudit,
    WarehousePurchaseRequest, WarehousePurchaseLine,
)
from django.db.models import Sum, Count
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import SalesPointStock, Transfer
try:
    from apps.inventory.models import StockMovement
except Exception:
    StockMovement = None

try:
    from apps.sales.models import SaleItem
except Exception:
    SaleItem = None



def _qty_maps_for(sp, product_ids=None):
    """Return dicts: sold_map, reserved_map, in_map, out_map for one salespoint."""
    product_ids = list(set(product_ids or [])) or None

    def _to_map(qs, key="product_id", agg="q"):
        return {r[key]: int(r.get(agg) or 0) for r in qs}

    sold_map = {}
    reserved_map = {}
    in_map = {}
    out_map = {}

    if SaleItem is not None:
        base = SaleItem.objects.filter(sale__salespoint=sp)
        if product_ids:
            base = base.filter(product_id__in=product_ids)
        sold_map = _to_map(
            base.filter(sale__status="approved").values("product_id").annotate(q=Sum("quantity"))
        )
        reserved_map = _to_map(
            base.filter(sale__status="awaiting_cashier").values("product_id").annotate(q=Sum("quantity"))
        )

    if Transfer is not None:
        t_base = Transfer.objects
        t_in_qs = t_base.filter(to_salespoint=sp)
        t_out_qs = t_base.filter(from_salespoint=sp)
        if product_ids:
            t_in_qs = t_in_qs.filter(product_id__in=product_ids)
            t_out_qs = t_out_qs.filter(product_id__in=product_ids)
        in_map = _to_map(t_in_qs.values("product_id").annotate(q=Sum("quantity")))
        out_map = _to_map(t_out_qs.values("product_id").annotate(q=Sum("quantity")))

    return sold_map, reserved_map, in_map, out_map


def _compute_remaining_for(sps):
    """opening + transfers_in − transfers_out − sold − reserved (floored at 0).
    If the model already has a denormalized remaining_qty, we show that directly.
    """
    opening = int(getattr(sps, "opening_qty", 0) or 0)
    if hasattr(sps, "remaining_qty") and sps.remaining_qty is not None:
        return int(max(0, int(sps.remaining_qty)))

    sold_map, reserved_map, in_map, out_map = _qty_maps_for(sps.salespoint, [sps.product_id])
    sold = int(sold_map.get(sps.product_id, 0))
    reserved = int(reserved_map.get(sps.product_id, 0))
    t_in = int(in_map.get(sps.product_id, 0))
    t_out = int(out_map.get(sps.product_id, 0))
    return max(0, opening + t_in - t_out - sold - reserved)

    

@admin.register(SalesPoint)
class SalesPointAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "phone", "is_warehouse", "short_address")
    list_filter = ("brand", "is_warehouse")
    search_fields = ("name", "phone", "address")

    def short_address(self, obj):
        if obj.address:
            return (obj.address[:60] + "…") if len(obj.address) > 60 else obj.address
        return ""
    short_address.short_description = "Adresse"

@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("salespoint", "product", "opening_qty", "quantity")
    # If your Product has product_type, keep it. Otherwise, change to: "product__kind"
    list_filter = ("salespoint", "product__product_type", "product__brand")
    # Removed product__sku to avoid errors now that SKU is optional/removed
    search_fields = ("product__name", "product__brand__name", "product__provider__name")

@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ("product", "from_salespoint", "to_salespoint", "quantity", "created_at")
    # Same note as above regarding product__product_type vs product__kind
    list_filter = ("from_salespoint", "to_salespoint", "product__product_type")
    search_fields = ("product__name", "from_salespoint__name", "to_salespoint__name")
    
    # Performance optimizations
    list_per_page = 50
    list_select_related = ("product", "from_salespoint", "to_salespoint")
    autocomplete_fields = ("product", "from_salespoint", "to_salespoint")
    date_hierarchy = "created_at"
    search_help_text = "Recherche par nom de produit ou nom de point de vente"


# === Transfer Requests (inter-salespoint) ===
class TransferRequestLineInline(admin.TabularInline):
    model = TransferRequestLine
    extra = 0
    fields = ("product", "quantity", "available_at_source")
    autocomplete_fields = ("product",)


@admin.register(TransferRequest)
class TransferRequestAdmin(admin.ModelAdmin):
    list_display = (
        "number", "from_salespoint", "to_salespoint", "requested_by",
        "status", "created_at", "sent_at",
        "line_count",
    )
    list_filter = ("status", "from_salespoint", "to_salespoint", "created_at")
    search_fields = (
        "number", "from_salespoint__name", "to_salespoint__name", "requested_by__username",
        "lines__product__name",
    )
    readonly_fields = ("created_at", "updated_at")
    inlines = [TransferRequestLineInline]
    autocomplete_fields = ("from_salespoint", "to_salespoint", "requested_by")
    
    # Performance optimizations
    list_per_page = 50
    list_select_related = ("from_salespoint", "to_salespoint", "requested_by")
    date_hierarchy = "created_at"
    search_help_text = "Recherche par numéro, nom de point de vente ou utilisateur"

    def line_count(self, obj):
        try:
            # Use cached count if available to avoid N+1 queries
            if hasattr(obj, '_lines_count'):
                return obj._lines_count
            return obj.lines.count()
        except Exception:
            return 0
    line_count.short_description = "Articles"
    
    def get_queryset(self, request):
        """Optimize queryset with prefetch_related to avoid N+1 queries."""
        qs = super().get_queryset(request)
        return qs.prefetch_related('lines')


# === Restock Requests (commercial director -> warehouse) ===
class RestockRequestItemInline(admin.TabularInline):
    model = RestockRequestItem
    extra = 0
    fields = ("product", "quantity", "cost_price", "wholesale_price", "selling_price", "total_cost", "quantity_validated", "validated_at", "validated_by")
    autocomplete_fields = ("product",)
    readonly_fields = ("validated_at", "total_cost")


@admin.register(RestockRequest)
class RestockRequestAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "salespoint", "provider", "requested_by", "status",
        "created_at", "total_amount", "item_count", "inbound_source", "tools",
    )
    list_filter = ("status", "salespoint", "provider", "created_at")
    search_fields = (
        "reference", "salespoint__name", "provider__name", "requested_by__username", "items__product__name",
    )
    readonly_fields = ("created_at", "updated_at", "reference", "total_amount")
    inlines = [RestockRequestItemInline]
    autocomplete_fields = ("salespoint", "provider", "requested_by")
    
    # Performance optimizations
    list_per_page = 50
    list_select_related = ("salespoint", "provider", "requested_by")
    date_hierarchy = "created_at"
    search_help_text = "Recherche par numéro de facture, point de vente, fournisseur ou utilisateur"

    def item_count(self, obj):
        try:
            # Use cached count if available to avoid N+1 queries
            if hasattr(obj, '_items_count'):
                return obj._items_count
            return obj.items.count()
        except Exception:
            return 0
    item_count.short_description = "Articles"
    
    def get_queryset(self, request):
        """Optimize queryset with prefetch_related to avoid N+1 queries."""
        qs = super().get_queryset(request)
        return qs.prefetch_related('items')

    def inbound_source(self, obj):
        ref = (obj.reference or "").upper()
        if ref.startswith("CD-"):
            return "CD → Entrepôt"
        if ref.startswith("WH-RQ-"):
            return "PDV → Entrepôt"
        return "—"
    inbound_source.short_description = "Origine"

    def tools(self, obj):
        links = []
        try:
            links.append(f'<a target="_blank" href="{reverse("inventory:warehouse_request_print", args=[obj.id])}">Imprimer</a>')
        except Exception:
            pass
        try:
            links.append(f'<a target="_blank" href="{reverse("inventory:api_wh_restock_lines", args=[obj.id])}">JSON</a>')
        except Exception:
            pass
        return mark_safe(" | ".join(links) or "—")
    tools.short_description = "Outils"

@admin.register(SalesPointStock)
class SalesPointStockAdmin(admin.ModelAdmin):
    list_display  = (
        "salespoint", "product",
        "opening_qty", "sold_qty", "transfer_in", "transfer_out",
        "reserved_qty", "remaining_qty", "remaining_display", "available_display", "alert_qty",
    )
    list_filter   = ("salespoint", "product__provider", "product__brand")
    search_fields = ("product__name", "product__brand__name", "product__provider__name")
    
    # Performance optimizations
    list_per_page = 50  # Reduce from default 100
    list_select_related = ("salespoint", "product", "product__brand", "product__provider")
    autocomplete_fields = ("salespoint", "product")
    search_help_text = "Recherche par nom de produit, marque ou fournisseur"
    
    # Add date hierarchy for better navigation
    date_hierarchy = "created_at" if hasattr(SalesPointStock, "created_at") else None

    def remaining_display(self, obj):
        opening = int(getattr(obj, "opening_qty", 0) or 0)
        tin = int(getattr(obj, "transfer_in", 0) or 0)
        tout = int(getattr(obj, "transfer_out", 0) or 0)
        sold = int(getattr(obj, "sold_qty", 0) or 0)
        resv = int(getattr(obj, "reserved_qty", 0) or 0)
        rem = opening + tin - tout - sold - resv
        return rem if rem > 0 else 0
    remaining_display.short_description = "Remaining (calc)"

    def available_display(self, obj):
        # Available = Remaining - Reserved (clamped at 0)
        opening = int(getattr(obj, "opening_qty", 0) or 0)
        tin = int(getattr(obj, "transfer_in", 0) or 0)
        tout = int(getattr(obj, "transfer_out", 0) or 0)
        sold = int(getattr(obj, "sold_qty", 0) or 0)
        resv = int(getattr(obj, "reserved_qty", 0) or 0)
        rem = opening + tin - tout - sold
        avail = rem - resv
        return avail if avail > 0 else 0
    available_display.short_description = "Available (now)"

@admin.register(StockTransaction)
class StockTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "salespoint", "product", "qty", "reason", "reference", "user")
    
    # Performance optimizations
    list_per_page = 100  # Higher for transactions since they're read-only
    list_select_related = ("salespoint", "product", "user")
    list_filter = ("reason", "salespoint", "created_at")
    search_fields = ("product__name", "salespoint__name", "reference")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
    search_help_text = "Recherche par nom de produit, point de vente ou référence"


@admin.register(RestockValidationAudit)
class RestockValidationAuditAdmin(admin.ModelAdmin):
    """Admin for restock validation audit trail."""
    list_display = (
        "validated_at", "restock_request", "product", "quantity_validated", 
        "stock_before_validation", "stock_after_validation", "total_value", "validated_by"
    )
    list_filter = ("validated_at", "restock_request__salespoint", "product__brand")
    search_fields = (
        "restock_request__reference", "product__name", "validated_by__username",
        "restock_request__salespoint__name"
    )
    readonly_fields = ("validated_at", "stock_before_validation", "stock_after_validation")
    date_hierarchy = "validated_at"
    
    # Performance optimizations
    list_per_page = 50
    list_select_related = ("restock_request", "product", "validated_by", "restock_request__salespoint")
    search_help_text = "Recherche par référence, produit, validateur ou point de vente"


# ===== Warehouse Purchase (Commande vers Directeur Commercial) =====
class WarehousePurchaseLineInline(admin.TabularInline):
    model = WarehousePurchaseLine
    extra = 0
    fields = ("product", "quantity_requested")
    autocomplete_fields = ("product",)


@admin.register(WarehousePurchaseRequest)
class WarehousePurchaseRequestAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "status", "requested_by", "created_at", "lines_count",
    )
    list_filter = ("status", "created_at", "requested_by")
    # Avoid expensive JOINs on very large datasets; search on reference and requester only
    search_fields = (
        "reference", "requested_by__username", "requested_by__first_name", "requested_by__last_name",
    )
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at")
    inlines = [WarehousePurchaseLineInline]
    list_per_page = 25
    # Major performance boost on SQLite/Postgres: avoid COUNT(*) of full result set
    show_full_result_count = False
    actions = ["action_mark_acknowledged", "action_export_csv"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Avoid heavy prefetch on very large datasets (can trigger SQLite expression depth limits)
        # Annotate count to display number of lines efficiently
        return qs.select_related("requested_by").annotate(_lines_count=Sum(0) + Count("lines"))

    def lines_count(self, obj):
        try:
            if hasattr(obj, "_lines_count") and obj._lines_count is not None:
                return obj._lines_count
            return obj.lines.count()
        except Exception:
            return 0
    lines_count.short_description = "Nb lignes"

    def action_mark_acknowledged(self, request, queryset):
        updated = queryset.filter(status__in=["sent"]).update(status="acknowledged")
        # Notify warehouse managers when a CMD-WH is acknowledged
        try:
            from django.contrib.auth import get_user_model
            from apps.sales.models import Notification
            User = get_user_model()
            for u in User.objects.filter(role='warehouse_mgr', is_active=True):
                Notification.objects.create(
                    user=u,
                    message=f"📨 CMD-WH accusée: {', '.join([obj.reference or str(obj.id) for obj in queryset])}",
                    link="/admin/inventory/warehousepurchaserequest/",
                    kind="cmd_wh_acknowledged",
                )
        except Exception:
            pass
        self.message_user(request, f"{updated} commande(s) marquées comme 'acknowledged'.")
    action_mark_acknowledged.short_description = "Marquer comme accusée (acknowledged)"

    def action_export_csv(self, request, queryset):
        return export_as_csv(self, request, queryset)
    action_export_csv.short_description = "Exporter CSV"

# === Additive admin enhancements (safe to append) ===
from django.contrib import admin
from django.apps import apps
from django.http import HttpResponse
import csv
from typing import Iterable


class StampedAdminMixin:
    """Adds created/updated fields to list_display if they exist on the model."""
    def get_list_display(self, request):
        base = list(super().get_list_display(request))
        for field in ("created_at", "updated_at", "created", "modified", "updated"):
            try:
                self.model._meta.get_field(field)
            except Exception:
                continue
            if field not in base:
                base.append(field)
        return tuple(base)

    def get_readonly_fields(self, request, obj=None):
        ro = list(getattr(super(), "get_readonly_fields", lambda *a, **k: [])(request, obj))
        for field in ("created_at", "updated_at", "created", "modified", "updated"):
            try:
                self.model._meta.get_field(field)
            except Exception:
                continue
            if field not in ro:
                ro.append(field)
        return tuple(ro)


def export_as_csv(modeladmin, request, queryset: Iterable):
    """Generic CSV export for current queryset. Only adds fields that exist."""
    model = modeladmin.model
    field_names = [f.name for f in model._meta.get_fields() if getattr(f, "concrete", False) and not getattr(f, "many_to_many", False)]

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f"attachment; filename={model._meta.model_name}_export.csv"

    writer = csv.writer(response)
    writer.writerow(field_names)
    for obj in queryset:
        row = []
        for name in field_names:
            val = getattr(obj, name, "")
            # Render FKs as their string repr instead of IDs
            if hasattr(val, "_meta"):
                val = str(val)
            row.append(val)
        writer.writerow(row)
    return response


export_as_csv.short_description = "Export selected to CSV"


class GenericInventoryAdmin(StampedAdminMixin, admin.ModelAdmin):
    list_per_page = 50
    actions = [export_as_csv]

    def get_search_fields(self, request):
        fields = list(getattr(super(), "get_search_fields", lambda *a, **k: [])(request))
        # Heuristics: include common text/code fields if present
        for name in ("name", "code", "sku", "barcode", "reference", "description"):
            try:
                self.model._meta.get_field(name)
            except Exception:
                continue
            if name not in fields:
                fields.append(name)
        # For FK lookups
        related_candidates = (
            ("product__name",),
            ("product__sku",),
            ("salespoint__name",),
            ("from_salespoint__name",),
            ("to_salespoint__name",),
            ("brand__name",),
            ("provider__name",),
        )
        for tpl in related_candidates:
            for look in tpl:
                # only add if left-most field exists somewhere to avoid admin warnings
                left = look.split("__")[0]
                try:
                    self.model._meta.get_field(left)
                except Exception:
                    continue
                if look not in fields:
                    fields.append(look)
        return tuple(dict.fromkeys(fields))  # dedupe while preserving order

    def get_list_filter(self, request):
        filters = list(getattr(super(), "get_list_filter", lambda *a, **k: [])(request))
        for name in ("status", "brand", "provider", "salespoint", "product"):
            try:
                self.model._meta.get_field(name)
            except Exception:
                continue
            if name not in filters:
                filters.append(name)
        # Date hierarchy helper
        if not getattr(self, "date_hierarchy", None):
            for name in ("created_at", "created", "date", "approved_at"):
                try:
                    self.model._meta.get_field(name)
                    self.date_hierarchy = name
                    break
                except Exception:
                    continue
        return tuple(filters)

    def get_autocomplete_fields(self, request):
        ac = list(getattr(super(), "get_autocomplete_fields", lambda *a, **k: [])(request))
        for name in ("product", "salespoint", "from_salespoint", "to_salespoint", "brand", "provider"):
            try:
                f = self.model._meta.get_field(name)
            except Exception:
                continue
            # Only add FKs/ManyToOne
            if getattr(f, "many_to_one", False) and name not in ac:
                ac.append(name)
        return tuple(ac)


# Attempt to attach these admins to common inventory models if they exist
for model_name in [
    "SalesPointStock",
    "Stock",
    "Transfer",
    "Inventory",
    "StockMovement",
    "Adjustment",
]:
    try:
        Model = apps.get_model("inventory", model_name)
    except LookupError:
        continue
    # If model is already registered, enhance it by subclassing its current admin; else use GenericInventoryAdmin
    if Model in admin.site._registry:
        # Already registered; leave the existing registration intact and just augment actions/search via mixin
        existing_admin = admin.site._registry[Model].__class__
        if not issubclass(existing_admin, StampedAdminMixin):
            class AugmentedAdmin(StampedAdminMixin, existing_admin):  # type: ignore[misc]
                actions = list(getattr(existing_admin, "actions", [])) + [export_as_csv]
            admin.site.unregister(Model)
            admin.site.register(Model, AugmentedAdmin)
    else:
        class AutoAdmin(GenericInventoryAdmin):
            pass
        admin.site.register(Model, AutoAdmin)

# Nice explicit tweaks for very common models if present
try:
    Transfer = apps.get_model("inventory", "Transfer")
    class TransferAdmin(GenericInventoryAdmin):
        def get_list_display(self, request):
            base = [
                x for x in (
                    "id", "reference", "from_salespoint", "to_salespoint", "status", "approved_at",
                ) if hasattr(self.model, x) or x == "id"
            ]
            return tuple(base) + super().get_list_display(request)

        def get_actions(self, request):
            actions = list(super().get_actions(request).keys())
            # Actions will be taken from functions bound to this class
            return super().get_actions(request)

        @admin.action(description="Mark selected as shipped")
        def mark_shipped(self, request, queryset):
            if not hasattr(Transfer, "status"):
                return
            updated = queryset.update(status=getattr(Transfer, "STATUS_SHIPPED", "shipped"))
            self.message_user(request, f"{updated} transfer(s) marked as shipped")

        @admin.action(description="Mark selected as received")
        def mark_received(self, request, queryset):
            if not hasattr(Transfer, "status"):
                return
            updated = queryset.update(status=getattr(Transfer, "STATUS_RECEIVED", "received"))
            self.message_user(request, f"{updated} transfer(s) marked as received")

        actions = [export_as_csv, "mark_shipped", "mark_received"]

    # If already registered from the generic loop, replace it with our tailored admin
    if Transfer in admin.site._registry:
        admin.site.unregister(Transfer)
    admin.site.register(Transfer, TransferAdmin)
except LookupError:
    pass

try:
    SalesPointStock = apps.get_model("inventory", "SalesPointStock")
    # If not already registered explicitly above, register a minimal generic admin.
    if SalesPointStock not in admin.site._registry:
        class SalesPointStockAutoAdmin(GenericInventoryAdmin):
            pass
        admin.site.register(SalesPointStock, SalesPointStockAutoAdmin)
except LookupError:
    pass