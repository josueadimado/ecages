from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    path('warehouse/', views.warehouse_dashboard, name='warehouse_dashboard'),
    path('warehouse/commande/', views.warehouse_commande, name='warehouse_commande'),
    path('warehouse/approvisionnement/', views.warehouse_inbound_cd, name='warehouse_inbound_cd'),
    path('warehouse/requests/', views.warehouse_requests, name='warehouse_requests'),
    path('warehouse/requests/<int:req_id>/lines/', views.warehouse_request_lines, name='warehouse_request_lines'),
    path('warehouse/requests/<int:req_id>/print/', views.warehouse_request_print, name='warehouse_request_print'),
    path('warehouse/journal/', views.warehouse_journal, name='warehouse_journal'),
    path('warehouse/transfers/history/', views.transfer_history, name='transfer_history'),
    path('warehouse/transfers/history/export.csv', views.transfer_history_export_csv, name='transfer_history_export_csv'),
    path('warehouse/restock-stats/', views.restock_stats, name='restock_stats'),
    path('warehouse/restock-stats/export.csv', views.restock_stats_export_csv, name='restock_stats_export_csv'),
    path('warehouse/stocks/', views.salespoints_stock, name='salespoints_stock'),
    path('warehouse/export-finished/', views.export_finished_products, name='export_finished_products'),
    path('warehouse/restock-history/', views.restock_history, name='restock_history'),
    path('warehouse/restock-journal/', views.warehouse_restock_journal, name='warehouse_restock_journal'),
    # Low-stock purchase builder
    path('warehouse/purchase/', views.warehouse_purchase_builder, name='warehouse_purchase_builder'),
    # APIs for warehouse restock
    path('api/wh/stock/', views.api_wh_stock, name='api_wh_stock'),
    path('api/wh/restock/send/', views.api_wh_restock_send, name='api_wh_restock_send'),
    path('api/wh/restock/validate/<int:req_id>/', views.api_wh_restock_validate, name='api_wh_restock_validate'),
    path('api/wh/restock/<int:req_id>/lines/', views.api_wh_restock_lines, name='api_wh_restock_lines'),
    path('api/wh/salespoints/', views.api_wh_salespoints, name='api_wh_salespoints'),
    path('api/wh/ref/', views.api_wh_ref, name='api_wh_ref'),
    # APIs for warehouse commande
    path('api/wh/cmd/ref/', views.api_wh_cmd_ref, name='api_wh_cmd_ref'),
    path('api/wh/cmd/submit/', views.api_wh_cmd_submit, name='api_wh_cmd_submit'),
    path('api/wh/cmd/save/', views.api_wh_cmd_save, name='api_wh_cmd_save'),
    path('api/wh/cmd/search/', views.api_wh_cmd_search_products, name='api_wh_cmd_search_products'),
    path('api/transfer/request/<int:req_id>/lines/', views.api_transfer_request_lines, name='api_transfer_request_lines'),
    
    # Barcode scanning and printing
    path('barcode-scanner/', views.barcode_scanner, name='barcode_scanner'),
    path('barcode-printer/', views.barcode_printer, name='barcode_printer'),
    path('api/scan-barcode/', views.api_scan_barcode, name='api_scan_barcode'),
    path('api/barcode-list/', views.api_barcode_list, name='api_barcode_list'),
    path('api/generate-barcodes/', views.api_generate_barcodes, name='api_generate_barcodes'),
    path('api/upload-proof-photo/', views.api_upload_proof_photo, name='api_upload_proof_photo'),
]