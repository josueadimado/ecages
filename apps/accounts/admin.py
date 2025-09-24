# apps/accounts/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from .models import User

@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "get_full_name", "role", "salespoint", "is_active", "is_staff")
    list_filter  = ("role", "salespoint", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)

    # Fields shown when editing a user
    fieldsets = (
        ("Identité", {"fields": ("username", "first_name", "last_name", "email")}),
        ("Rôle & Point de vente", {"fields": ("role", "salespoint")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates importantes", {"fields": ("last_login", "date_joined")}),
    )

    # Fields shown when creating a user
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "username", "first_name", "last_name", "email",
                "role", "salespoint",
                "password1", "password2",
                "is_active", "is_staff", "is_superuser", "groups"
            ),
        }),
    )