from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="products_index"),
    path("api/search/", views.api_search, name="api_search"),
]