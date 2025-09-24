# apps/reports/models.py
from django.conf import settings
from django.db import models

class CashierDailyReport(models.Model):
    STATUS_CHOICES = [
        ("pending", "En attente"),
        ("approved", "Approuvée"),
        ("rejected", "Rejetée"),
    ]

    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cashier_reports")
    salespoint = models.ForeignKey("inventory.SalesPoint", on_delete=models.PROTECT, related_name="cashier_reports")
    report_date = models.DateField(db_index=True)
    total_amount = models.PositiveBigIntegerField(help_text="Montant total des ventes (FCFA)")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True)

    note = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("cashier", "salespoint", "report_date")]
        ordering = ["-report_date", "-created_at"]
        verbose_name = "Rapport de caisse (quotidien)"
        verbose_name_plural = "Rapports de caisse (quotidiens)"

    def __str__(self):
        return f"{self.report_date} – {self.salespoint} – {self.cashier} – {self.total_amount} FCFA"