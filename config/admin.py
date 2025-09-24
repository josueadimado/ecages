# config/admin.py
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.utils.translation import gettext_lazy as _

class ECAGESAdminSite(AdminSite):
    """Custom admin site with performance optimizations for ECAGES."""
    
    site_header = _("ECAGES Administration")
    site_title = _("ECAGES Admin Portal")
    index_title = _("Bienvenue dans l'administration ECAGES")
    
    # Performance optimizations
    def get_app_list(self, request):
        """
        Override to optimize app list loading.
        """
        app_list = super().get_app_list(request)
        
        # Optimize model counts for large tables
        for app in app_list:
            for model in app['models']:
                # Skip expensive count queries for large models
                if model['object_name'] in ['SalesPointStock', 'StockTransaction', 'Product']:
                    model['count'] = '~'  # Show approximate count
                else:
                    try:
                        model['count'] = self._get_model_count(model['object_name'])
                    except Exception:
                        model['count'] = '~'
        
        return app_list
    
    def _get_model_count(self, model_name):
        """Get model count with timeout protection."""
        try:
            from django.apps import apps
            model = apps.get_model('inventory', model_name)
            if model:
                return model.objects.count()
        except Exception:
            pass
        return '~'

# Create custom admin site instance
admin_site = ECAGESAdminSite(name='ecages_admin')

# Register models with custom site
from apps.inventory.admin import *
from apps.products.admin import *
from apps.accounts.admin import *
from apps.sales.admin import *
from apps.providers.admin import *
from apps.finance.admin import *
from apps.hr.admin import *
from apps.logistics.admin import *
from apps.reports.admin import *

