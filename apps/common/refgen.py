from django.utils import timezone
from django.db import transaction


def _next_sequence(prefix: str, values: list[str]) -> str:
    max_seq = 0
    for ref in values:
        try:
            num = int(str(ref).split('-')[-1])
        except Exception:
            num = 0
        max_seq = max(max_seq, num)
    return f"{prefix}{max_seq + 1:04d}"


def generate_wh_rq(model_cls):
    """Generate WH-RQ-DDMMYY-XXXX for RestockRequest-like model."""
    today = timezone.localdate()
    prefix = f"WH-RQ-{today.strftime('%d%m%y')}-"
    with transaction.atomic():
        values = list(model_cls.objects.select_for_update().filter(reference__startswith=prefix).values_list('reference', flat=True))
        return _next_sequence(prefix, values)


def generate_cmd_wh(model_cls):
    """Generate CMD-WH-DDMMYY-XXXX for WarehousePurchaseRequest-like model."""
    today = timezone.localdate()
    prefix = f"CMD-WH-{today.strftime('%d%m%y')}-"
    with transaction.atomic():
        values = list(model_cls.objects.select_for_update().filter(reference__startswith=prefix).values_list('reference', flat=True))
        return _next_sequence(prefix, values)



