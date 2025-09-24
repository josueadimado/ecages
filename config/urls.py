from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView  # <-- add this import


urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dashboard", permanent=False)),  # <-- add this line
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("providers/", include("apps.providers.urls")),
    path("products/", include("apps.products.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("sales/", include(("apps.sales.urls", "sales"), namespace="sales")),
    path("finance/", include("apps.finance.urls")),
    path("hr/", include("apps.hr.urls")),
    path("logistics/", include("apps.logistics.urls")),
    path("reports/", include("apps.reports.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)