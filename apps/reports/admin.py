# apps/reports/admin.py
from django.contrib import admin
from .models import CashierDailyReport

@admin.register(CashierDailyReport)
class CashierDailyReportAdmin(admin.ModelAdmin):
    list_display = ("report_date", "salespoint", "cashier", "total_amount", "status", "created_at")
    list_filter = ("status", "salespoint", "cashier", "report_date")
    search_fields = ("cashier__username", "cashier__first_name", "cashier__last_name", "salespoint__name")
    date_hierarchy = "report_date"