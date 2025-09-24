# apps/providers/views.py
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET
from .models import Provider

def index(request):
    return HttpResponse("Providers app OK")

@require_GET
def api_providers(request):
    """Return list of active providers for dropdowns."""
    providers = Provider.objects.filter(is_active=True).order_by('name')
    return JsonResponse({
        'providers': [
            {
                'id': provider.id,
                'name': provider.name,
                'contact': provider.contact,
                'email': provider.email
            }
            for provider in providers
        ]
    })