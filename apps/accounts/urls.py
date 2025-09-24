# apps/accounts/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("api/users-by-role/", views.users_by_role, name="users_by_role"),

    # add these:
    path("users/", views.users_list, name="accounts_users"),
    path("users/new/", views.user_create, name="accounts_user_create"),
    path("users/<int:pk>/edit/", views.user_edit, name="accounts_user_edit"),
]