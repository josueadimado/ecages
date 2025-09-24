from django.urls import path

from .views import manager as manager_views

urlpatterns = [
    path('manager/dashboard/', manager_views.dashboard, name='manager_dashboard_modular'),
]



