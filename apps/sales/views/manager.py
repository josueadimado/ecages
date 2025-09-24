from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.common.permissions import is_manager_role


@login_required
def dashboard(request):
    role = getattr(request.user, 'role', '')
    if not (request.user.is_superuser or is_manager_role(role)):
        return redirect('sales:dashboard')
    return render(request, 'sales/manager/manager_dashboard.html')


