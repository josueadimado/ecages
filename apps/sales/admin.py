# apps/sales/admin.py
from django.contrib import admin
from .models import Sale, SaleItem, CancellationRequest, Notification

class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "salespoint",
        "seller",
        "cashier",
        "status",
        "total_amount",
        "received_amount",
        "approved_at",
        "created_at",
    )
    list_display_links = ("number",)
    list_filter = ("status", "salespoint", "seller", "cashier")
    search_fields = (
        "number",
        "customer_name",
        "customer_phone",
        "seller__username",
        "cashier__username",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 50
    inlines = [SaleItemInline]
    readonly_fields = ("approved_at",)

@admin.register(CancellationRequest)
class CancellationRequestAdmin(admin.ModelAdmin):
    list_display = ("sale", "requested_by", "status", "created_at")
    list_filter = ("status",)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "message", "kind", "created_at", "read_at")
    list_filter = ("kind", "created_at", "read_at", "user")
    search_fields = ("message", "user__username")