# Package initializer for modular sales views (commercial, manager, cashier)
# Also load legacy views from apps/sales/views.py to preserve existing imports

import os
import importlib.util

_pkg_dir = os.path.dirname(__file__)                # .../apps/sales/views/
_sales_dir = os.path.dirname(_pkg_dir)              # .../apps/sales/
_legacy_path = os.path.join(_sales_dir, 'views.py')

if os.path.isfile(_legacy_path):
    spec = importlib.util.spec_from_file_location('apps.sales.legacy_views', _legacy_path)
    if spec and spec.loader:  # type: ignore
        _legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_legacy)  # type: ignore
        # Export all public attributes from legacy module at package level
        for _name in dir(_legacy):
            if not _name.startswith('_'):
                globals()[_name] = getattr(_legacy, _name)


