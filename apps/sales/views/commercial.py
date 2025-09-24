import json
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q, Sum, Count
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.db import transaction
from django.utils import timezone

from apps.common.notifications import notify_role
from apps.products.models import Product
from apps.inventory.models import RestockRequest, RestockRequestItem
from apps.providers.models import Provider


@login_required
def api_commercial_price_update(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Méthode invalide.'}, status=405)
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return JsonResponse({'ok': False, 'error': 'Non autorisé.'}, status=403)
    try:
        data = json.loads(request.body.decode('utf-8'))
        product_id = int(data.get('product_id') or 0)
        cost_price = float(data.get('cost_price') or 0)
        wholesale_price = float(data.get('wholesale_price') or 0)
        selling_price = float(data.get('selling_price') or 0)
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Données invalides.'}, status=400)

    if product_id <= 0:
        return JsonResponse({'ok': False, 'error': 'Produit manquant.'}, status=400)

    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Produit introuvable.'}, status=404)

    msg = (
        f"Changement de prix demandé pour {product.name}: "
        f"coût={cost_price}, gros={wholesale_price}, vente={selling_price}"
    )
    notify_role('stock_mgr', msg, link='', kind='price_change_request')
    return JsonResponse({'ok': True})


@login_required
def api_commercial_restock(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Méthode invalide.'}, status=405)
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return JsonResponse({'success': False, 'message': 'Non autorisé.'}, status=403)
    
    try:
        data = json.loads(request.body.decode('utf-8'))
        provider_id = int(data.get('provider_id') or 0)
        invoice_number = (data.get('invoice_number') or '').strip()
        kind = (data.get('kind') or 'piece').strip()
        products = data.get('products', [])
    except Exception as e:
        return JsonResponse({'success': False, 'message': 'Données invalides.'}, status=400)

    if provider_id <= 0:
        return JsonResponse({'success': False, 'message': 'Fournisseur requis.'}, status=400)
    
    if not products:
        return JsonResponse({'success': False, 'message': 'Aucun produit sélectionné.'}, status=400)

    try:
        provider = Provider.objects.get(pk=provider_id, is_active=True)
    except Provider.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Fournisseur introuvable.'}, status=404)

    # Get warehouse
    from apps.inventory.models import SalesPoint
    warehouse = SalesPoint.objects.filter(is_warehouse=True).first()
    if not warehouse:
        return JsonResponse({'success': False, 'message': 'Entrepôt introuvable.'}, status=404)

    try:
        with transaction.atomic():
            # Create restock request
            total_amount = sum(float(p.get('total_cost', 0)) for p in products)
            
            # Generate proper sequential invoice number if not provided
            if not invoice_number:
                today = timezone.now().date()
                type_code = 'M' if kind == 'moto' else 'P'
                date_str = today.strftime('%y%m%d')
                
                # Get the last invoice number for today and type
                last_invoice = RestockRequest.objects.filter(
                    reference__startswith=f'CD-{date_str}-{type_code}-',
                    requested_by__role='commercial_dir'
                ).order_by('-reference').first()
                
                if last_invoice and last_invoice.reference:
                    # Extract the sequence number and increment
                    try:
                        last_seq = int(last_invoice.reference.split('-')[-1])
                        next_seq = last_seq + 1
                    except (ValueError, IndexError):
                        next_seq = 1
                else:
                    next_seq = 1
                
                invoice_number = f'CD-{date_str}-{type_code}-{next_seq:04d}'
            
            restock_request = RestockRequest.objects.create(
                salespoint=warehouse,
                requested_by=request.user,
                provider=provider,
                status='sent',
                reference=invoice_number,
                total_amount=Decimal(str(total_amount)),
                sent_at=timezone.now()
            )
            
            # Create restock items
            for product_data in products:
                product_id = int(product_data.get('id', 0))
                quantity = int(product_data.get('quantity', 0))
                cost_price = Decimal(str(product_data.get('cost_price', 0)))
                wholesale_price = Decimal(str(product_data.get('wholesale_price', 0)))
                selling_price = Decimal(str(product_data.get('selling_price', 0)))
                
                if product_id <= 0 or quantity <= 0:
                    continue
                
                try:
                    product = Product.objects.get(pk=product_id, is_active=True)
                except Product.DoesNotExist:
                    continue
                
                RestockRequestItem.objects.create(
                    request=restock_request,
                    product=product,
                    quantity=quantity,
                    cost_price=cost_price,
                    wholesale_price=wholesale_price,
                    selling_price=selling_price,
                    total_cost=quantity * cost_price
                )
            
            # Notify warehouse manager
            type_label = "Motos" if kind == 'moto' else "Pièces"
            msg = f"Réapprovisionnement {type_label} en attente de validation • {provider.name} • {invoice_number} • {len(products)} produit(s)"
            notify_role('warehouse_mgr', msg, link='/inventory/warehouse/requests/', kind='commercial_restock_request')
            
            return JsonResponse({
                'success': True, 
                'message': 'Demande de réapprovisionnement envoyée avec succès.',
                'request_id': restock_request.id,
                'invoice_number': invoice_number
            })
            
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Erreur lors de la création: {str(e)}'}, status=500)




@login_required
def commercial_journal(request):
    """Journal des approvisionnements réalisés par le Directeur Commercial."""
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return redirect('sales:dashboard')

    qs = (
        RestockRequest.objects.select_related('provider', 'requested_by', 'salespoint')
        .filter(requested_by=request.user)
        .order_by('-created_at')
    )

    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip()
    provider_id_raw = (request.GET.get('provider') or '').strip()
    provider_id = int(provider_id_raw) if provider_id_raw.isdigit() else 0
    df = parse_date(request.GET.get('from') or '')
    dt = parse_date(request.GET.get('to') or '')
    if not df and not dt:
        today = timezone.localdate()
        df = dt = today

    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(invoice_number__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if provider_id > 0:
        qs = qs.filter(provider_id=provider_id)
    if df:
        qs = qs.filter(created_at__date__gte=df)
    if dt:
        qs = qs.filter(created_at__date__lte=dt)

    totals = qs.aggregate(total_amount=Sum('total_amount'), count=Count('id'))

    try:
        per_raw = int(request.GET.get('per') or 50)
    except Exception:
        per_raw = 50
    per = per_raw if per_raw in {25, 50, 100} else 50
    paginator = Paginator(qs, per)
    page_number = request.GET.get('page') or 1
    try:
        page_obj = paginator.get_page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.get_page(1)

    providers = Provider.objects.filter(is_active=True).order_by('name')

    context = {
        'rows': page_obj.object_list,
        'paginator': paginator,
        'page_obj': page_obj,
        'totals': totals,
        'q': q,
        'status': status,
        'provider_id': provider_id,
        'date_from': df.isoformat() if df else '',
        'date_to': dt.isoformat() if dt else '',
        'per': per,
        'providers': providers,
        'back_url': reverse('sales:commercial_dashboard'),
    }
    return render(request, 'sales/commercial/commercial_journal.html', context)
