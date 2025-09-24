from django.urls import path, include
from . import views as sales_views
from apps.inventory import models as inv_models  # ensure app is loaded for new models

# Optional: only include receipt route if the view exists to avoid import-time errors
try:
    from .views import print_receipt as _print_receipt
except (ImportError, AttributeError):
    _print_receipt = None

# Namespace for reverse() calls elsewhere
app_name = "sales"

urlpatterns = [
    # Sales staff dashboard & post-login routing
    # Primary modern name (use 'sales:dashboard')
    path("dashboard/", sales_views.sales_dashboard, name="dashboard"),
    path("manager-dashboard/", sales_views.manager_dashboard, name="manager_dashboard"),
    path("commercial-dashboard/", sales_views.commercial_dashboard, name="commercial_dashboard"),
    path("commercial-dashboard/table/", sales_views.commercial_products_table_partial, name="commercial_products_table_partial"),
    # Modularized role routes
    path("", include("apps.sales.urls_commercial")),
    path("", include("apps.sales.urls_manager")),
    path("", include("apps.sales.urls_cashier")),
    path("post-login/", sales_views.post_login_redirect, name="post_login_redirect"),

    # Public APIs used by the sales UI
    path("api/products/", sales_views.api_products_for_sale, name="sales_api_products"),
    path("api/clients/", sales_views.api_clients, name="sales_api_clients"),
    path("api/invoice-number/", sales_views.api_invoice_number, name="sales_api_invoice_number"),
    path("api/create-sale-draft/", sales_views.api_create_sale_draft, name="sales_api_create_sale_draft"),
    path("api/cancel-request/", sales_views.api_cancellation_request, name="sales_api_cancel"),
    # Cancellation workflow (salesperson + accounting)
    path("api/find-sale-by-number/", sales_views.api_find_sale_by_number, name="sales_api_find_sale_by_number"),
    path("api/cancel-sale-immediate/", sales_views.api_cancel_sale_immediate, name="sales_api_cancel_sale_immediate"),
    path("api/cancel-sale-request/", sales_views.api_cancel_sale_request, name="sales_api_cancel_sale_request"),
    path("api/accounting/approve-cancellation/", sales_views.api_accounting_approve_cancellation, name="sales_api_accounting_approve_cancellation"),

    # Cashier workflow
    path("cashier-dashboard/", sales_views.cashier_dashboard, name="cashier_dashboard"),
    path("cashier/journal/", sales_views.cashier_journal, name="cashier_journal"),
    # Manager journal
    path("manager/journal/", sales_views.manager_journal, name="manager_journal"),
    path("manager/restock/", sales_views.manager_restock, name="manager_restock"),
    path("manager/inbound/", sales_views.manager_inbound_transfers, name="manager_inbound"),
    path("manager/transfers/history/", sales_views.manager_transfer_history, name="manager_transfer_history"),
    path("manager/transfer-request/", sales_views.manager_transfer_request, name="manager_transfer_request"),
    path("manager/transfer-inbox/", sales_views.manager_transfer_inbox, name="manager_transfer_inbox"),
    path("api/manager/restock-request/<int:request_id>/lines/", sales_views.api_manager_restock_lines, name="manager_restock_lines"),
    path("api/manager/restock-request/<int:request_id>/validate/", sales_views.api_manager_validate_restock, name="manager_validate_restock"),

    path("api/manager/transfer/<int:transfer_id>/ack/", sales_views.api_manager_ack_transfer, name="manager_ack_transfer"),
    path("api/manager/transfers/ack/", sales_views.api_manager_ack_transfers, name="manager_ack_transfers"),
    path("api/manager/restock-search/", sales_views.api_manager_restock_search, name="manager_restock_search"),
    path("api/manager/source-stocks/", sales_views.api_manager_source_stocks, name="manager_source_stocks"),
    path("api/manager/transfer-request/save/", sales_views.api_manager_save_transfer_request, name="manager_save_transfer_request"),
    path("api/manager/transfer-request/allocate-number/", sales_views.api_manager_tr_allocate, name="manager_tr_allocate"),
    path("api/manager/transfer-request/<int:req_id>/lines/", sales_views.api_manager_tr_lines, name="manager_tr_lines"),
    path("api/manager/transfer-request/<int:req_id>/decide/", sales_views.api_manager_tr_decide, name="manager_tr_decide"),
    path("api/manager/transfer-updates/", sales_views.api_manager_transfer_updates, name="manager_transfer_updates"),
    path("api/notifications/", sales_views.api_notifications, name="notifications_list"),
    path("api/notifications/read/", sales_views.api_notifications_mark_read, name="notifications_mark_read"),
    path("cashier/report/", sales_views.cashier_report, name="cashier_report"),
    path("api/cashier/pending/", sales_views.api_cashier_pending, name="sales_api_cashier_pending"),
    path("api/cashier/sale/<int:sale_id>/", sales_views.api_cashier_sale_detail, name="sales_api_cashier_sale_detail"),
    path("api/cashier/sale/<int:sale_id>/validate/", sales_views.api_cashier_validate, name="sales_api_cashier_validate"),
    path("api/cashier/sale/<int:sale_id>/cancel/", sales_views.api_cashier_cancel, name="sales_api_cashier_cancel"),
    path("api/cashier/sales-summary/", sales_views.api_cashier_sales_summary, name="sales_api_cashier_sales_summary"),
]

# Add receipt route only when implemented
if _print_receipt is not None:
    urlpatterns.append(
        path("receipt/<int:sale_id>/", _print_receipt, name="sales_print_receipt")
    )