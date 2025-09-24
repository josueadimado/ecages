from django.urls import path

from .views import cashier as cashier_views

urlpatterns = [
    path('cashier/dashboard/', cashier_views.dashboard, name='cashier_dashboard_modular'),
]



