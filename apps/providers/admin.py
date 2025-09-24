from django.contrib import admin
from .models import Provider, Brand

class BrandInline(admin.TabularInline):
    model = Brand
    extra = 0

@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "contact", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "email", "contact")
    inlines = [BrandInline]

@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "note")
    list_filter = ("provider",)
    search_fields = ("name", "provider__name")