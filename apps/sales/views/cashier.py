from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render


@login_required
def dashboard(request):
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'cashier'):
        return redirect('sales:dashboard')
    return render(request, 'sales/cashier/cashier_dashboard.html')


