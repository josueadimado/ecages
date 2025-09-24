def is_manager_role(role: str) -> bool:
    return role in ("sales_manager", "manager", "gerant")


def is_warehouse_mgr(role: str) -> bool:
    return role == "warehouse_mgr"


def is_commercial_dir(role: str) -> bool:
    return role == "commercial_dir"



