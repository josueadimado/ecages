# apps/providers/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="providers_index"),
    path("api/providers/", views.api_providers, name="api_providers"),
]