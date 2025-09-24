from django.apps import AppConfig

class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"           # <-- IMPORTANT
    verbose_name = "Comptes & Rôles"