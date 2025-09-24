"""
Management command to generate daily reports for stock movements, variances, and alerts.
This can be run via cron job to send reports via email and WhatsApp.
"""
import json
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.db.models import Q, Sum, Count, F
from apps.inventory.models import (
    SalesPoint, SalesPointStock, StockTransaction, RestockRequest, 
    CycleCount, CycleCountLine
)
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem


class Command(BaseCommand):
    help = 'Generate and send daily reports via email and WhatsApp'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            help='Date to generate report for (YYYY-MM-DD). Defaults to yesterday.'
        )
        parser.add_argument(
            '--email',
            action='store_true',
            help='Send email reports'
        )
        parser.add_argument(
            '--whatsapp',
            action='store_true',
            help='Send WhatsApp reports'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Generate reports without sending them'
        )

    def handle(self, *args, **options):
        report_date = self._get_report_date(options.get('date'))
        send_email = options.get('email', True)
        send_whatsapp = options.get('whatsapp', True)
        dry_run = options.get('dry_run', False)
        
        self.stdout.write(f'Generating daily report for {report_date}')
        
        # Generate report data
        report_data = self._generate_report_data(report_date)
        
        if dry_run:
            self.stdout.write('DRY RUN - Report data generated:')
            self.stdout.write(json.dumps(report_data, indent=2, default=str))
            return
        
        # Send reports
        if send_email:
            self._send_email_report(report_data, report_date)
        
        if send_whatsapp:
            self._send_whatsapp_report(report_data, report_date)
        
        self.stdout.write(
            self.style.SUCCESS(f'Daily report for {report_date} completed successfully')
        )

    def _get_report_date(self, date_str):
        """Get the report date, defaulting to yesterday."""
        if date_str:
            try:
                return timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.stdout.write(
                    self.style.ERROR(f'Invalid date format: {date_str}. Use YYYY-MM-DD')
                )
                return (timezone.now() - timedelta(days=1)).date()
        return (timezone.now() - timedelta(days=1)).date()

    def _generate_report_data(self, report_date):
        """Generate comprehensive report data for the given date."""
        start_date = timezone.datetime.combine(report_date, timezone.datetime.min.time())
        end_date = timezone.datetime.combine(report_date, timezone.datetime.max.time())
        
        # Stock movements
        movements = StockTransaction.objects.filter(
            created_at__date=report_date
        ).select_related('salespoint', 'product', 'user')
        
        # Sales summary
        sales = Sale.objects.filter(
            created_at__date=report_date,
            status='approved'
        ).select_related('salespoint', 'cashier')
        
        # Restock requests
        restock_requests = RestockRequest.objects.filter(
            created_at__date=report_date
        ).select_related('salespoint', 'requested_by')
        
        # Cycle counts
        cycle_counts = CycleCount.objects.filter(
            count_date=report_date
        ).select_related('salespoint', 'counted_by')
        
        # Low stock alerts - we need to calculate available_qty in Python since it's a property
        all_stocks = SalesPointStock.objects.select_related('salespoint', 'product').all()
        low_stock_items = []
        for stock in all_stocks:
            if 0 < stock.available_qty <= stock.alert_qty:
                low_stock_items.append(stock)
        
        # Stock variances from cycle counts
        variances = CycleCountLine.objects.filter(
            cycle_count__count_date=report_date,
            variance__isnull=False
        ).exclude(variance=0).select_related('cycle_count__salespoint', 'product')
        
        return {
            'date': report_date.isoformat(),
            'summary': {
                'total_movements': movements.count(),
                'total_sales': sales.count(),
                'total_sales_value': sum(sale.total_amount for sale in sales),
                'total_restock_requests': restock_requests.count(),
                'cycle_counts_completed': cycle_counts.filter(status='completed').count(),
                'low_stock_items': len(low_stock_items),
                'variances_found': variances.count(),
            },
            'movements': [
                {
                    'salespoint': mov.salespoint.name,
                    'product': mov.product.name,
                    'qty': mov.qty,
                    'reason': mov.get_reason_display(),
                    'reference': mov.reference,
                    'user': mov.user.username if mov.user else 'System',
                    'time': mov.created_at.strftime('%H:%M'),
                }
                for mov in movements[:50]  # Limit to 50 most recent
            ],
            'sales_by_salespoint': [
                {
                    'salespoint': sp_name,
                    'count': sales.filter(salespoint__name=sp_name).count(),
                    'total_value': sum(s.total_amount for s in sales.filter(salespoint__name=sp_name)),
                }
                for sp_name in sales.values_list('salespoint__name', flat=True).distinct()
            ],
            'restock_requests': [
                {
                    'id': req.id,
                    'reference': req.reference or f'REQ-{req.id}',
                    'salespoint': req.salespoint.name,
                    'status': req.get_status_display(),
                    'lines_count': req.lines.count(),
                    'requested_by': req.requested_by.username,
                }
                for req in restock_requests
            ],
            'low_stock_alerts': [
                {
                    'salespoint': item.salespoint.name,
                    'product': item.product.name,
                    'available_qty': item.available_qty,
                    'alert_qty': item.alert_qty,
                    'shortage': item.alert_qty - item.available_qty,
                }
                for item in low_stock_items
            ],
            'variances': [
                {
                    'salespoint': var.cycle_count.salespoint.name,
                    'product': var.product.name,
                    'expected_qty': var.expected_qty,
                    'actual_qty': var.actual_qty,
                    'variance': var.variance,
                    'notes': var.notes,
                }
                for var in variances
            ],
        }

    def _send_email_report(self, report_data, report_date):
        """Send email report to configured recipients."""
        try:
            subject = f'Rapport quotidien ECAGES - {report_date}'
            
            # Generate HTML report
            html_content = self._generate_html_report(report_data)
            
            # Get recipients from settings
            recipients = getattr(settings, 'DAILY_REPORT_EMAILS', [])
            if not recipients:
                self.stdout.write(
                    self.style.WARNING('No email recipients configured. Set DAILY_REPORT_EMAILS in settings.')
                )
                return
            
            send_mail(
                subject=subject,
                message='',  # Plain text version
                html_message=html_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipients,
                fail_silently=False,
            )
            
            self.stdout.write(f'Email report sent to {len(recipients)} recipients')
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Failed to send email report: {str(e)}')
            )

    def _send_whatsapp_report(self, report_data, report_date):
        """Send WhatsApp report to configured recipients."""
        try:
            # Generate short summary for WhatsApp
            summary = report_data['summary']
            message = f"""📊 *Rapport ECAGES - {report_date}*

📈 *Ventes*: {summary['total_sales']} commandes ({summary['total_sales_value']:,.0f} FCFA)
📦 *Mouvements*: {summary['total_movements']} transactions
🔄 *Demandes*: {summary['total_restock_requests']} réapprovisionnements
⚠️ *Stock bas*: {summary['low_stock_items']} articles
📋 *Inventaires*: {summary['cycle_counts_completed']} terminés
🔍 *Écarts*: {summary['variances_found']} variances

Voir le rapport complet par email."""
            
            # Get WhatsApp recipients from settings
            recipients = getattr(settings, 'DAILY_REPORT_WHATSAPP', [])
            if not recipients:
                self.stdout.write(
                    self.style.WARNING('No WhatsApp recipients configured. Set DAILY_REPORT_WHATSAPP in settings.')
                )
                return
            
            # In production, you'd use WhatsApp Business API or Twilio
            # For now, just log the message
            self.stdout.write(f'WhatsApp message for {len(recipients)} recipients:')
            self.stdout.write(message)
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Failed to send WhatsApp report: {str(e)}')
            )

    def _generate_html_report(self, report_data):
        """Generate HTML content for the email report."""
        summary = report_data['summary']
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .header {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
                .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }}
                .summary-item {{ background: #e9ecef; padding: 15px; border-radius: 6px; text-align: center; }}
                .summary-value {{ font-size: 24px; font-weight: bold; color: #007bff; }}
                .summary-label {{ font-size: 14px; color: #6c757d; margin-top: 5px; }}
                table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
                th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #dee2e6; }}
                th {{ background: #f8f9fa; font-weight: bold; }}
                .alert {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 10px; border-radius: 4px; margin: 10px 0; }}
                .variance-positive {{ color: #28a745; }}
                .variance-negative {{ color: #dc3545; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>📊 Rapport quotidien ECAGES</h1>
                <p>Date: {report_data['date']}</p>
            </div>
            
            <div class="summary">
                <div class="summary-item">
                    <div class="summary-value">{summary['total_sales']}</div>
                    <div class="summary-label">Ventes</div>
                </div>
                <div class="summary-item">
                    <div class="summary-value">{summary['total_sales_value']:,.0f} FCFA</div>
                    <div class="summary-label">Chiffre d'affaires</div>
                </div>
                <div class="summary-item">
                    <div class="summary-value">{summary['total_movements']}</div>
                    <div class="summary-label">Mouvements de stock</div>
                </div>
                <div class="summary-item">
                    <div class="summary-value">{summary['total_restock_requests']}</div>
                    <div class="summary-label">Demandes de réapprovisionnement</div>
                </div>
                <div class="summary-item">
                    <div class="summary-value">{summary['low_stock_items']}</div>
                    <div class="summary-label">Articles en stock bas</div>
                </div>
                <div class="summary-item">
                    <div class="summary-value">{summary['variances_found']}</div>
                    <div class="summary-label">Variances détectées</div>
                </div>
            </div>
        """
        
        # Add low stock alerts
        if report_data['low_stock_alerts']:
            html += """
            <h2>⚠️ Alertes de stock bas</h2>
            <table>
                <tr><th>Point de vente</th><th>Produit</th><th>Stock disponible</th><th>Seuil d'alerte</th><th>Manque</th></tr>
            """
            for alert in report_data['low_stock_alerts']:
                html += f"""
                <tr>
                    <td>{alert['salespoint']}</td>
                    <td>{alert['product']}</td>
                    <td>{alert['available_qty']}</td>
                    <td>{alert['alert_qty']}</td>
                    <td>{alert['shortage']}</td>
                </tr>
                """
            html += "</table>"
        
        # Add variances
        if report_data['variances']:
            html += """
            <h2>🔍 Variances d'inventaire</h2>
            <table>
                <tr><th>Point de vente</th><th>Produit</th><th>Attendu</th><th>Réel</th><th>Écart</th></tr>
            """
            for var in report_data['variances']:
                variance_class = "variance-positive" if var['variance'] > 0 else "variance-negative"
                html += f"""
                <tr>
                    <td>{var['salespoint']}</td>
                    <td>{var['product']}</td>
                    <td>{var['expected_qty']}</td>
                    <td>{var['actual_qty']}</td>
                    <td class="{variance_class}">{var['variance']:+d}</td>
                </tr>
                """
            html += "</table>"
        
        html += """
        </body>
        </html>
        """
        
        return html

