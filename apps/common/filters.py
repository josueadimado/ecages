from datetime import datetime
from django.utils import timezone


def parse_date_range(request):
    """Return (start_date, end_date) defaulting to today if none provided."""
    du = (request.GET.get('du') or '').strip()
    au = (request.GET.get('au') or '').strip()
    if not du or not au:
        today = timezone.localdate()
        return today, today
    try:
        start = datetime.strptime(du, "%Y-%m-%d").date()
        end = datetime.strptime(au, "%Y-%m-%d").date()
        return start, end
    except Exception:
        today = timezone.localdate()
        return today, today



