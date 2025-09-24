from typing import Iterable, Optional

from django.contrib.auth import get_user_model

try:
    from apps.sales.models import Notification
except Exception:
    Notification = None  # type: ignore


def notify_users(users: Iterable, message: str, link: str = "", kind: Optional[str] = None) -> int:
    """Create simple notifications for a list of users.

    Returns the number of notifications created. Fails silently if model missing.
    """
    if Notification is None:
        return 0
    created = 0
    for user in users:
        try:
            Notification.objects.create(user=user, message=message, link=link, kind=kind or "info")
            created += 1
        except Exception:
            # Keep silent to avoid breaking flows
            pass
    return created


def notify_role(role: str, message: str, link: str = "", kind: Optional[str] = None) -> int:
    """Notify all active users with a given role."""
    User = get_user_model()
    users = User.objects.filter(role=role, is_active=True)
    return notify_users(users, message, link, kind)



