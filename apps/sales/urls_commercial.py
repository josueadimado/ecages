from django.urls import path
from importlib import import_module

# Avoid clash between module file apps.sales.views and package apps.sales.views/
commercial_views = import_module('apps.sales.views.commercial')

urlpatterns = [
    path("api/commercial/price-update/", commercial_views.api_commercial_price_update, name="api_commercial_price_update"),
    path("api/commercial/restock/", commercial_views.api_commercial_restock, name="api_commercial_restock"),
]


