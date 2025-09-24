from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import login, logout
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods
from django import forms
from .forms import LoginForm
from .models import User
from django.db.models import Q
from .forms import SimpleUserCreateForm, SimpleUserAssignForm

def is_manager(user):
    # allow superusers and key roles to manage users
    return user.is_superuser or user.role in (
        "admin", "sales_manager", "commercial_dir", "ceo", "hr"
    )


@login_required
def dashboard(request):
    # Redirections par rôle (utiliser des noms d'URL namespacés)
    role = getattr(request.user, "role", "") or ""

    if role in ("sales", "sales_manager"):
        return redirect("sales:dashboard")

    if role == "cashier":
        return redirect("sales:cashier_dashboard")

    if role == "commercial_dir":
        return redirect("sales:commercial_dashboard")

    if role == "warehouse_mgr" or getattr(request.user, "is_staff", False):
        # Redirect warehouse keepers to the warehouse dashboard
        try:
            return redirect("inventory:warehouse_dashboard")
        except Exception:
            pass

    # TODO: autres rôles (warehouse, accounting, etc.)
    return render(request, "accounts/dashboard.html")

@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = LoginForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            # authenticated user attached by form.clean()
            login(request, form.user_obj)
            return redirect("dashboard")

    return render(request, "accounts/login.html", {"form": form})


@require_http_methods(["GET", "POST"])
def logout_view(request):
    """Log out the user on GET or POST and redirect to login page."""
    logout(request)
    return redirect("login")

@require_GET
def users_by_role(request):
    role = request.GET.get("role")
    if not role:
        return HttpResponseBadRequest("Missing role")

    qs = User.objects.filter(role=role, is_active=True).order_by("first_name", "last_name", "username")
    data = [
        {
            "username": u.username,
            "display": f"{u.first_name} {u.last_name}".strip() or u.username
        }
        for u in qs
    ]
    return JsonResponse(data, safe=False)

@login_required
@user_passes_test(is_manager)
def users_list(request):
    q = (request.GET.get("q") or "").strip()
    users = User.objects.select_related("salespoint").all().order_by("username")
    if q:
        users = users.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q)
        )
    return render(request, "accounts/users_list.html", {"users": users, "q": q})

@login_required
@user_passes_test(is_manager)
def user_create(request):
    if request.method == "POST":
        form = SimpleUserCreateForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("accounts_users")
    else:
        form = SimpleUserCreateForm()
    return render(request, "accounts/user_form.html", {"form": form, "title": "Créer un utilisateur"})

@login_required
@user_passes_test(is_manager)
def user_edit(request, pk):
    u = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = SimpleUserAssignForm(request.POST, instance=u)
        if form.is_valid():
            form.save()
            return redirect("accounts_users")
    else:
        form = SimpleUserAssignForm(instance=u)
    return render(request, "accounts/user_form.html", {"form": form, "title": f"Modifier: {u.username}"})