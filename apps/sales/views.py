import json
from datetime import date
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q, F, Max
from django.core.exceptions import FieldDoesNotExist
from django.http import HttpResponse

# --- Helpers to keep SalesPointStock denormalized counters in sync (optional fields) ---
def _compute_stock_maps(sp, product_ids=None):
    filt = {}
    if product_ids:
        filt = {"product_id__in": list(set(product_ids))}

    sold_per_product = (
        SaleItem.objects.filter(sale__salespoint=sp, sale__status="approved", **filt)
        .values("product_id").annotate(qty_sold=Sum("quantity"))
    )
    sold_map = {r["product_id"]: int(r["qty_sold"] or 0) for r in sold_per_product}

    pending_per_product = (
        SaleItem.objects.filter(sale__salespoint=sp, sale__status="awaiting_cashier", **filt)
        .values("product_id").annotate(qty_res=Sum("quantity"))
    )
    reserved_map = {r["product_id"]: int(r["qty_res"] or 0) for r in pending_per_product}

    out_map = {
        r["product_id"]: int(r["q"] or 0)
        for r in Transfer.objects.filter(from_salespoint=sp, **filt)
        .values("product_id").annotate(q=Sum("quantity"))
    }
    in_map = {
        r["product_id"]: int(r["q"] or 0)
        for r in Transfer.objects.filter(to_salespoint=sp, **filt)
        .values("product_id").annotate(q=Sum("quantity"))
    }
    return sold_map, reserved_map, in_map, out_map

def _update_salespoint_stock_denorm(sp, product_ids):
    """Update optional denormalized fields (e.g., remaining_qty, sold_qty) on SalesPointStock.
    Only updates fields that exist on the model; otherwise, it safely does nothing.
    """
    if not product_ids:
        return
    product_ids = list(set(product_ids))
    sold_map, reserved_map, in_map, out_map = _compute_stock_maps(sp, product_ids)

    sps_rows = (
        SalesPointStock.objects.select_for_update()
        .filter(salespoint=sp, product_id__in=product_ids)
        .select_related("product")
    )
    for sps in sps_rows:
        opening = int(sps.opening_qty or 0)
        sold = int(sold_map.get(sps.product_id, 0))
        reserved = int(reserved_map.get(sps.product_id, 0))
        t_in = int(in_map.get(sps.product_id, 0))
        t_out = int(out_map.get(sps.product_id, 0))
        remaining = opening + t_in - t_out - sold - reserved
        if remaining < 0:
            remaining = 0

        updates = {}
        # Update only if these fields exist on the model
        try:
            sps._meta.get_field("remaining_qty")
            updates["remaining_qty"] = remaining
        except FieldDoesNotExist:
            pass
        try:
            sps._meta.get_field("sold_qty")
            updates["sold_qty"] = sold
        except FieldDoesNotExist:
            pass

        if updates:
            SalesPointStock.objects.filter(pk=sps.pk).update(**updates)
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.db import IntegrityError
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from urllib.parse import urlencode
from django.forms.models import model_to_dict

# Import models for restock validation
from apps.inventory.models import RestockRequest, RestockLine, SalesPoint, SalesPointStock, StockTransaction
def _is_manager_role(role: str) -> bool:
    """Return True if the provided role string corresponds to a salespoint manager.
    Accepts several aliases in FR/EN commonly used in this project.
    """
    if not role:
        return False
    r = (role or "").strip().lower()
    aliases = {
        "sales_manager", "salespoint_manager",
        "sale_resp", "sales_resp", "sale resp", "sale_responsable",
        "responsable", "responsable point de vente",
        "responsable_point_de_vente", "responsable_pdv", "resp_pdv", "pdv_manager",
        "gerant", "gérant", "gerant_pdv", "gerant pdv",
    }
    if r in aliases:
        return True
    # Fuzzy contains checks (safe keywords)
    for tok in ("manager", "gérant", "gerant", "responsable", "pdv", "resp"):
        if tok in r:
            return True
    return False

from django.urls import reverse
from django.utils.dateparse import parse_date   # <-- add this line
from apps.inventory.models import SalesPointStock, Transfer
from apps.inventory.models import RestockRequest, RestockLine, TransferRequest, TransferRequestLine, SalesPoint
from apps.products.models import Product
from apps.providers.models import Provider
from .models import Notification
from .models import Sale, SaleItem, CancellationRequest
from .services import (
    SaleError,
    create_sale_draft,
    generate_invoice_number,
    find_sale_by_number,
    cancel_sale_same_day,
    create_cancellation_request,
    approve_cancellation_request,
)

from apps.reports.models import CashierDailyReport


def _is_cashier(user):
    return user.is_superuser or getattr(user, "role", "") == "cashier"


@login_required
def post_login_redirect(request):
    """Route users to the right dashboard immediately after login."""
    user = request.user
    role = getattr(user, "role", "") or ""

    if user.is_superuser:
        # choose your preferred destination for superusers:
        return redirect("dashboard")  # or redirect("admin:index") if using Django admin

    if role == "cashier":
        return redirect("sales:cashier_dashboard")

    if _is_manager_role(role):
        return redirect("sales:manager_dashboard")

    if role == "commercial_dir":
        return redirect("sales:commercial_dashboard")

    if role == "sales":
        return redirect("sales:dashboard")

    # Fallback (general/home dashboard)
    return redirect("dashboard")


@login_required
def sales_dashboard(request):
    # Send cashiers away from here
    if getattr(request.user, "role", "") == "cashier":
        return redirect("sales:cashier_dashboard")

    role = getattr(request.user, "role", "")
    # Only plain sales (or superuser) can access this page
    if request.user.is_superuser:
        pass
    elif _is_manager_role(role):
        return redirect("sales:manager_dashboard")
    elif role == "commercial_dir":
        return redirect("sales:commercial_dashboard")
    elif role != "sales":
        return redirect("dashboard")

    sp = request.user.salespoint
    q = (request.GET.get("q") or "").strip()

    if not sp:
        return render(
            request, "sales/dashboard.html",
            {
                "salespoint": None,
                "pieces": [], "motos": [],
                "today_sales": [], "today_total": "0.00",
                "pending_cancels": [],
                "msg": "Aucun point de vente n'est associé à votre compte.",
                "q": q,
            },
        )

    stocks = (
        SalesPointStock.objects
        .filter(salespoint=sp, product__is_active=True)
        .select_related("product", "product__brand")
        .order_by("product__name")
    )
    if q:
        stocks = stocks.filter(
            Q(product__name__icontains=q) |
            Q(product__brand__name__icontains=q)
        )

    sold_per_product = (
        SaleItem.objects.filter(sale__salespoint=sp, sale__status="approved")
        .values("product_id")
        .annotate(qty_sold=Sum("quantity"))
    )
    sold_map = {r["product_id"]: (r["qty_sold"] or 0) for r in sold_per_product}

    pending_per_product = (
        SaleItem.objects
        .filter(sale__salespoint=sp, sale__status="awaiting_cashier")
        .values("product_id")
        .annotate(qty_res=Sum("quantity"))
    )
    reserved_map = {r["product_id"]: (r["qty_res"] or 0) for r in pending_per_product}

    out_map = {
        r["product_id"]: (r["q"] or 0)
        for r in Transfer.objects.filter(from_salespoint=sp)
        .values("product_id").annotate(q=Sum("quantity"))
    }
    in_map = {
        r["product_id"]: (r["q"] or 0)
        for r in Transfer.objects.filter(to_salespoint=sp)
        .values("product_id").annotate(q=Sum("quantity"))
    }

    def classify(product: Product) -> str:
        name = (product.name or "").lower()
        if "moto" in name:
            return "moto"
        return product.product_type or "piece"

    def row(sps: SalesPointStock):
        p = sps.product
        # Normalize quantities to integers
        opening = int(sps.opening_qty or 0)
        sold = int(sold_map.get(p.id, 0) or 0)
        reserved = int(reserved_map.get(p.id, 0) or 0)
        t_out = int(out_map.get(p.id, 0) or 0)
        t_in  = int(in_map.get(p.id, 0) or 0)
        # Compute remaining dynamically, including reservations that are awaiting cashier
        remaining = opening + t_in - t_out - sold - reserved
        if remaining < 0:
            remaining = 0
        return {
            "product_id": p.id,
            "name": p.name,
            "brand": p.brand.name if p.brand_id else "",
            "retail_price": p.selling_price,
            "opening_qty": opening,
            "sold_qty": sold,
            "reserved_qty": reserved,
            "remaining_qty": remaining,  # computed live
            "transfer_out": t_out,
            "transfer_in": t_in,
            "type": classify(p),
        }

    rows = [row(sps) for sps in stocks]
    pieces = [r for r in rows if r["type"] == "piece"]
    motos  = [r for r in rows if r["type"] == "moto"]

    today = timezone.localdate()
    todays_sales = (
        Sale.objects.filter(salespoint=sp, created_at__date=today)
        .annotate(items_count=Count("items"))
        .order_by("-created_at")
    )
    today_total = todays_sales.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    pending_cancels = (
        CancellationRequest.objects
        .filter(sale__salespoint=sp, status="pending", created_at__date=today)
        .select_related("sale", "requested_by")
        .order_by("-created_at")
    )

    return render(
        request, "sales/dashboard.html",
        {
            "salespoint": sp,
            "pieces": pieces,
            "motos": motos,
            "today_sales": todays_sales,
            "today_total": today_total,
            "pending_cancels": pending_cancels,
            "msg": None,
            "q": q,
        },
    )


@login_required
def manager_dashboard(request):
    """Dashboard for salespoint manager (gérant).
    Includes sales features + stock alerts and recent inbound transfers.
    """
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("dashboard")

    sp = getattr(request.user, "salespoint", None)
    q = (request.GET.get("q") or "").strip()

    if not sp:
        return render(
            request, "sales/dashboard.html",
            {
                "salespoint": None,
                "pieces": [], "motos": [],
                "today_sales": [], "today_total": Decimal("0.00"),
                "pending_cancels": [],
                "msg": "Aucun point de vente n'est associé à votre compte.",
                "q": q,
            },
        )

    stocks = (
        SalesPointStock.objects
        .filter(salespoint=sp, product__is_active=True)
        .select_related("product", "product__brand")
        .order_by("product__name")
    )
    if q:
        stocks = stocks.filter(
            Q(product__name__icontains=q) | Q(product__brand__name__icontains=q)
        )

    sold_per_product = (
        SaleItem.objects.filter(sale__salespoint=sp, sale__status="approved")
        .values("product_id").annotate(qty_sold=Sum("quantity"))
    )
    sold_map = {r["product_id"]: (r["qty_sold"] or 0) for r in sold_per_product}

    pending_per_product = (
        SaleItem.objects
        .filter(sale__salespoint=sp, sale__status="awaiting_cashier")
        .values("product_id").annotate(qty_res=Sum("quantity"))
    )
    reserved_map = {r["product_id"]: (r["qty_res"] or 0) for r in pending_per_product}

    out_map = {
        r["product_id"]: (r["q"] or 0)
        for r in Transfer.objects.filter(from_salespoint=sp)
        .values("product_id").annotate(q=Sum("quantity"))
    }
    in_map = {
        r["product_id"]: (r["q"] or 0)
        for r in Transfer.objects.filter(to_salespoint=sp)
        .values("product_id").annotate(q=Sum("quantity"))
    }

    def classify(product: Product) -> str:
        name = (product.name or "").lower()
        if "moto" in name:
            return "moto"
        return product.product_type or "piece"

    def row(sps: SalesPointStock):
        p = sps.product
        opening = int(sps.opening_qty or 0)
        sold = int(sold_map.get(p.id, 0) or 0)
        reserved = int(reserved_map.get(p.id, 0) or 0)
        t_out = int(out_map.get(p.id, 0) or 0)
        t_in  = int(in_map.get(p.id, 0) or 0)
        remaining = opening + t_in - t_out - sold - reserved
        if remaining < 0:
            remaining = 0
        return {
            "product_id": p.id,
            "name": p.name,
            "brand": p.brand.name if p.brand_id else "",
            "retail_price": p.selling_price,
            "opening_qty": opening,
            "sold_qty": sold,
            "reserved_qty": reserved,
            "remaining_qty": remaining,
            "transfer_out": t_out,
            "transfer_in": t_in,
            "alert_qty": int(getattr(sps, "alert_qty", 0) or 0),
            "type": classify(p),
        }

    rows = [row(sps) for sps in stocks]
    pieces = [r for r in rows if r["type"] == "piece"]
    motos  = [r for r in rows if r["type"] == "moto"]

    # Today sales summary and list (same as salesperson dashboard)
    today = timezone.localdate()
    todays_sales = (
        Sale.objects.filter(salespoint=sp, created_at__date=today)
        .annotate(items_count=Count("items"))
        .order_by("-created_at")
    )
    today_total = todays_sales.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    # Pending cancellation requests today
    pending_cancels = (
        CancellationRequest.objects
        .filter(sale__salespoint=sp, status="pending", created_at__date=today)
        .select_related("sale", "requested_by")
        .order_by("-created_at")
    )

    # Render the same template as salesperson for identical look & features
    return render(
        request, "sales/dashboard.html",
        {
            "salespoint": sp,
            "pieces": pieces,
            "motos": motos,
            "today_sales": todays_sales,
            "today_total": today_total,
            "pending_cancels": pending_cancels,
            "msg": None,
            "q": q,
        },
    )


@login_required
@require_GET
def api_products_for_sale(request):
    sp = request.user.salespoint
    if not sp:
        return JsonResponse([], safe=False)

    ptype = request.GET.get("type")
    q = (request.GET.get("q") or "").strip()
    qs = (
        SalesPointStock.objects
        .filter(salespoint=sp, product__is_active=True)
        .select_related("product", "product__brand")
        .order_by("product__name")
    )
    if ptype in ("piece", "moto"):
        qs = qs.filter(product__product_type=ptype)
    if q:
        qs = qs.filter(
            Q(product__name__icontains=q) |
            Q(product__brand__name__icontains=q)
        )

    # Precompute dynamic per-product aggregates for availability (mirror of dashboard logic)
    sold_per_product = (
        SaleItem.objects.filter(sale__salespoint=sp, sale__status="approved")
        .values("product_id")
        .annotate(qty_sold=Sum("quantity"))
    )
    sold_map = {r["product_id"]: (r["qty_sold"] or 0) for r in sold_per_product}

    pending_per_product = (
        SaleItem.objects
        .filter(sale__salespoint=sp, sale__status="awaiting_cashier")
        .values("product_id")
        .annotate(qty_res=Sum("quantity"))
    )
    reserved_map = {r["product_id"]: (r["qty_res"] or 0) for r in pending_per_product}

    out_map = {
        r["product_id"]: (r["q"] or 0)
        for r in Transfer.objects.filter(from_salespoint=sp)
        .values("product_id").annotate(q=Sum("quantity"))
    }
    in_map = {
        r["product_id"]: (r["q"] or 0)
        for r in Transfer.objects.filter(to_salespoint=sp)
        .values("product_id").annotate(q=Sum("quantity"))
    }

    data = []
    for sps in qs[:500]:  # protect payload
        p = sps.product
        # Normalize quantities to integers
        opening = int(sps.opening_qty or 0)
        sold = int(sold_map.get(p.id, 0) or 0)
        reserved = int(reserved_map.get(p.id, 0) or 0)
        t_out = int(out_map.get(p.id, 0) or 0)
        t_in  = int(in_map.get(p.id, 0) or 0)
        # Compute remaining dynamically, including reservations awaiting cashier
        remaining = opening + t_in - t_out - sold - reserved
        if remaining < 0:
            remaining = 0
        data.append({
            "product_id": p.id,
            "name": p.name,
            "brand": p.brand.name if p.brand_id else "",
            "price": int(p.selling_price or 0),
            "available": remaining,
        })
    return JsonResponse(data, safe=False)


@login_required
@require_GET
def api_invoice_number(request):
    sp = request.user.salespoint
    if not sp:
        return HttpResponseBadRequest("Aucun point de vente.")
    kind = (request.GET.get("kind") or "P").upper()
    return JsonResponse({"number": generate_invoice_number(sp, kind)})


@login_required
@require_GET
def api_clients(request):
    q = (request.GET.get("q") or "").strip()
    base = Sale.objects.all()
    if q:
        base = base.filter(Q(customer_name__icontains=q) | Q(customer_phone__icontains=q))
    rows = base.order_by().values("customer_name", "customer_phone").distinct()[:20]
    data = [{"name": r["customer_name"], "phone": r["customer_phone"] or ""} for r in rows]
    return JsonResponse(
        [{"name": "DIVERS", "phone": ""}, {"name": "SAV", "phone": ""}] + data,
        safe=False,
    )


@login_required
@require_POST
def api_create_sale_draft(request):
    sp = request.user.salespoint
    if not sp:
        return HttpResponseBadRequest("Aucun point de vente.")
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("Requête invalide.")

    # Extract standard fields
    kind = (payload.get("kind") or "P").upper()
    customer_name = payload.get("customer_name") or "DIVERS"
    customer_phone = payload.get("customer_phone") or ""
    payment_type = payload.get("payment_type") or "cash"
    items = payload.get("items") or []

    # NEW: Extract extended details for moto sales (optional)
    details = payload.get("customer_details") or {}
    chassis_number = details.get("chassis_number", "")
    engine_number = details.get("engine_number", "")
    amount_in_words = details.get("amount_in_words", "")

    # Server-side guard: one moto per sale (qty = 1)
    if kind == "M":
        if len(items) != 1:
            return JsonResponse({"ok": False, "error": "Une vente de moto doit contenir une seule moto (Qté = 1)."}, status=400)
        try:
            q1 = int(items[0].get("qty") or 0)
        except Exception:
            q1 = 0
        if q1 != 1:
            return JsonResponse({"ok": False, "error": "La quantité d'une moto doit être 1."}, status=400)

    try:
        # First try: pass the extra fields to the domain service if it supports them
        try:
            sale = create_sale_draft(
                salespoint=sp,
                seller=request.user,
                kind=kind,
                customer_name=customer_name,
                customer_phone=customer_phone,
                payment_type=payment_type,
                items=items,
                customer_details=details,
                chassis_number=chassis_number,
                engine_number=engine_number,
                amount_in_words=amount_in_words,
            )
        except TypeError:
            # Fallback: older service signature — call without extras, then persist directly if fields exist
            sale = create_sale_draft(
                salespoint=sp,
                seller=request.user,
                kind=kind,
                customer_name=customer_name,
                customer_phone=customer_phone,
                payment_type=payment_type,
                items=items,
            )
            dirty_fields = []
            if hasattr(sale, "customer_details"):
                sale.customer_details = details; dirty_fields.append("customer_details")
            if hasattr(sale, "chassis_number"):
                sale.chassis_number = chassis_number; dirty_fields.append("chassis_number")
            if hasattr(sale, "engine_number"):
                sale.engine_number = engine_number; dirty_fields.append("engine_number")
            if hasattr(sale, "amount_in_words"):
                sale.amount_in_words = amount_in_words; dirty_fields.append("amount_in_words")
            if dirty_fields:
                sale.save(update_fields=dirty_fields)

        return JsonResponse({
            "ok": True,
            "sale_id": sale.id,
            "number": sale.number,
            "total": int(sale.total_amount),
        })
    except SaleError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Erreur inattendue."}, status=500)


@login_required
@require_POST
def api_cancellation_request(request):
    sale_id = request.POST.get("sale_id")
    reason = (request.POST.get("reason") or request.POST.get("motif") or "").strip()
    if not reason:
        return JsonResponse({"ok": False, "error": "Motif d'annulation requis."}, status=400)
    sale = get_object_or_404(Sale, pk=sale_id)
    cr = CancellationRequest.objects.create(sale=sale, requested_by=request.user, reason=reason)
    return JsonResponse({"ok": True, "request_id": cr.id})


@login_required
def cashier_dashboard(request):
    # Only cashiers (or superusers) can see this page
    if not _is_cashier(request.user):
        return redirect("sales:dashboard")  # bounce non-cashiers to sales dashboard
    sp = getattr(request.user, "salespoint", None)
    return render(request, "sales/cashier/cashier_dashboard.html", {"salespoint": sp})

@login_required
def cashier_journal(request):
    """
    List all sales visible to the cashier (their salespoint by default),
    with lightweight filters for query, status, and date range.
    """
    if not _is_cashier(request.user):
        return redirect("sales:dashboard")

    sp = getattr(request.user, "salespoint", None)

    qs = (
        Sale.objects.select_related("seller", "salespoint")
        .order_by("-created_at")
    )
    if sp and not request.user.is_superuser:
        qs = qs.filter(salespoint=sp)

    # filters
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    # Parse incoming dates; default to today if both missing
    df = parse_date(request.GET.get("from") or "")
    dt = parse_date(request.GET.get("to") or "")
    if not df and not dt:
        today = timezone.localdate()
        df = dt = today

    if q:
        qs = qs.filter(
            Q(number__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(seller__first_name__icontains=q) |
            Q(seller__last_name__icontains=q) |
            Q(seller__username__icontains=q)
        )

    if status in {"draft", "awaiting_cashier", "approved", "rejected", "cancelled"}:
        qs = qs.filter(status=status)

    if df:
        qs = qs.filter(created_at__date__gte=df)
    if dt:
        qs = qs.filter(created_at__date__lte=dt)

    # quick aggregates (optional in the template)
    totals = qs.aggregate(
        total_amount=Sum("total_amount"),
        count=Count("id"),
    )

    # Pagination
    try:
        per_raw = int(request.GET.get("per") or 50)
    except Exception:
        per_raw = 50
    allowed_per = {25, 50, 100}
    per = per_raw if per_raw in allowed_per else 50

    paginator = Paginator(qs, per)
    page_number = request.GET.get("page") or 1
    try:
        page_obj = paginator.get_page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.get_page(1)

    rows = page_obj.object_list

    # Normalize values for the template inputs (YYYY-MM-DD)
    date_from_val = df.isoformat() if df else (request.GET.get("from") or "")
    date_to_val   = dt.isoformat() if dt else (request.GET.get("to") or "")

    # Build base querystring preserving filters (excluding page)
    base_params = {
        "q": q or "",
        "status": status or "",
        "from": date_from_val or "",
        "to": date_to_val or "",
        "per": str(per),
    }
    # Drop empty values
    base_params = {k: v for k, v in base_params.items() if v}
    base_qs = urlencode(base_params)

    context = {
        "salespoint": sp,
        "q": q,
        "status": status,
        "date_from": date_from_val,
        "date_to": date_to_val,
        "per": per,
        "rows": rows,
        "totals": totals,
        # pagination
        "paginator": paginator,
        "page_obj": page_obj,
        "base_qs": base_qs,
    }
    return render(request, "sales/cashier/cashier_journal.html", context)


@login_required
def manager_journal(request):
    """Sales journal for salespoint managers.
    Same filters and layout as cashier journal, but restricted to manager roles.
    """
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")

    sp = getattr(request.user, "salespoint", None)

    qs = (
        Sale.objects.select_related("seller", "salespoint")
        .order_by("-created_at")
    )
    if sp and not request.user.is_superuser:
        qs = qs.filter(salespoint=sp)

    # filters
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    df = parse_date(request.GET.get("from") or "")
    dt = parse_date(request.GET.get("to") or "")
    if not df and not dt:
        today = timezone.localdate()
        df = dt = today

    if q:
        qs = qs.filter(
            Q(number__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(seller__first_name__icontains=q) |
            Q(seller__last_name__icontains=q) |
            Q(seller__username__icontains=q)
        )

    if status in {"draft", "awaiting_cashier", "approved", "rejected", "cancelled"}:
        qs = qs.filter(status=status)

    if df:
        qs = qs.filter(created_at__date__gte=df)
    if dt:
        qs = qs.filter(created_at__date__lte=dt)

    totals = qs.aggregate(
        total_amount=Sum("total_amount"),
        count=Count("id"),
    )

    # Pagination
    try:
        per_raw = int(request.GET.get("per") or 50)
    except Exception:
        per_raw = 50
    allowed_per = {25, 50, 100}
    per = per_raw if per_raw in allowed_per else 50

    paginator = Paginator(qs, per)
    page_number = request.GET.get("page") or 1
    try:
        page_obj = paginator.get_page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.get_page(1)

    rows = page_obj.object_list

    date_from_val = df.isoformat() if df else (request.GET.get("from") or "")
    date_to_val   = dt.isoformat() if dt else (request.GET.get("to") or "")

    base_params = {
        "q": q or "",
        "status": status or "",
        "from": date_from_val or "",
        "to": date_to_val or "",
        "per": str(per),
    }
    base_params = {k: v for k, v in base_params.items() if v}
    base_qs = urlencode(base_params)

    context = {
        "salespoint": sp,
        "q": q,
        "status": status,
        "date_from": date_from_val,
        "date_to": date_to_val,
        "per": per,
        "rows": rows,
        "totals": totals,
        "paginator": paginator,
        "page_obj": page_obj,
        "base_qs": base_qs,
        "back_url": reverse("sales:manager_dashboard"),
    }
    return render(request, "sales/manager/manager_journal.html", context)


@login_required
@csrf_exempt  # CSRF exempt for JSON POST from same-origin manager page
def manager_restock(request):
    """Restock request builder for salespoint managers.
    - GET: show current draft (or auto-populated from alert thresholds) with search to add items.
    - POST JSON: save draft lines; action=send to mark as sent to warehouse.
    """
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")

    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return render(request, "sales/manager/manager_restock.html", {"salespoint": None, "rows": [], "draft": None, "msg": "Aucun point de vente lié."})

    # Load or create latest draft
    draft = RestockRequest.objects.filter(salespoint=sp, status="draft").order_by("-created_at").first()
    if not draft:
        draft = RestockRequest.objects.create(salespoint=sp, requested_by=request.user, status="draft")

    if request.method == "POST":
        try:
            try:
                payload = json.loads(request.body.decode("utf-8"))
            except Exception:
                return JsonResponse({"ok": False, "error": "Requête invalide."})

            action = (payload.get("action") or "save").lower()
            lines = payload.get("lines") or []  # [{product_id, qty}]
            duplicates_removed = []  # Always define for both save and send paths
            # Replace lines
            with transaction.atomic():
                RestockLine.objects.filter(request=draft).delete()
                # Deduplicate by product and aggregate quantities to avoid unique constraint errors
                pid_to_qty = {}
                for ln in lines:
                    pid = int(ln.get("product_id") or 0)
                    qty = int(ln.get("qty") or 0) or 1
                    if pid <= 0 or qty <= 0:
                        continue
                    pid_to_qty[pid] = pid_to_qty.get(pid, 0) + qty

                for pid, qty in pid_to_qty.items():
                    sps = SalesPointStock.objects.filter(salespoint=sp, product_id=pid).first()
                    RestockLine.objects.create(
                        request=draft,
                        product_id=pid,
                        quantity=qty,
                        remaining_qty=int(getattr(sps, "remaining_qty", 0) if sps else 0),
                        alert_qty=int(getattr(sps, "alert_qty", 0) if sps else 0),
                    )
                # Send action
                if action == "send":
                    # Prevent duplicate products across pending requests for this salespoint
                    pending_reqs = RestockRequest.objects.filter(
                        salespoint=sp, status__in=["sent", "partially_validated"]
                    ).prefetch_related("lines")
                    already_pending_pids = set()
                    for req in pending_reqs:
                        already_pending_pids.update(req.lines.values_list("product_id", flat=True))

                    duplicates_removed = []
                    if already_pending_pids:
                        # Collect product info to report back
                        from apps.products.models import Product
                        dup_lines = RestockLine.objects.filter(request=draft, product_id__in=already_pending_pids)
                        prod_map = {p.id: p for p in Product.objects.filter(id__in=dup_lines.values_list("product_id", flat=True))}
                        for dl in dup_lines:
                            p = prod_map.get(dl.product_id)
                            duplicates_removed.append({
                                "product_id": dl.product_id,
                                "name": getattr(p, "name", f"#{dl.product_id}"),
                                "qty": int(getattr(dl, "quantity", 0) or 0),
                            })
                        dup_lines.delete()

                    # Ensure there remains at least one line
                    if not RestockLine.objects.filter(request=draft).exists():
                        return JsonResponse({
                            "ok": False,
                            "error": "Tous les produits sélectionnés sont déjà en attente d'approvisionnement. Ajoutez de nouveaux produits.",
                            "code": "all_duplicates",
                            "duplicates_removed": duplicates_removed,
                        }, status=400)
                    # Assign a human-friendly reference if missing (e.g., WH-RQ-DDMMYY-0001)
                    if not (draft.reference or '').strip():
                        today = timezone.localdate()
                        prefix = f"WH-RQ-{today.strftime('%d%m%y')}-"
                        # Lock and compute next sequence safely
                        with transaction.atomic():
                            max_seq = 0
                            for ref in RestockRequest.objects.select_for_update().filter(reference__startswith=prefix).values_list('reference', flat=True):
                                try:
                                    num = int(str(ref).split('-')[-1])
                                except Exception:
                                    num = 0
                                max_seq = max(max_seq, num)
                            draft.reference = f"{prefix}{max_seq + 1:04d}"

                    draft.status = "sent"
                    draft.sent_at = timezone.now()
                    draft.save(update_fields=["status", "sent_at", "reference"])
                    # Notify warehouse managers
                    try:
                        from django.contrib.auth import get_user_model
                        from apps.sales.models import Notification
                        User = get_user_model()
                        mgrs = User.objects.filter(role="warehouse_mgr")
                        qs_lines = RestockLine.objects.filter(request=draft).select_related("product")
                        line_count = qs_lines.count()
                        # Summary notification
                        for u in mgrs:
                            Notification.objects.create(
                                user=u,
                                message=f"Nouvelle demande de réapprovisionnement: {sp.name} (→ {line_count} produit(s))",
                                link="/inventory/warehouse/requests/",
                                kind="restock_incoming"
                            )
                    except Exception:
                        pass
            return JsonResponse({"ok": True, "draft_id": draft.id, "status": draft.status, "duplicates_removed": duplicates_removed})
        except Exception as e:
            return JsonResponse({"ok": False, "error": str(e) or "Erreur serveur."})

    # Build suggested rows (below or equal alert) and hide items already pending in another request
    sps_rows = (
        SalesPointStock.objects.select_related("product", "product__brand")
        .filter(salespoint=sp)
        .order_by("product__name")
    )
    # Gather already pending product ids
    pending_pids = set(
        RestockLine.objects.filter(
            request__salespoint=sp,
            request__status__in=["sent", "partially_validated"],
        ).values_list("product_id", flat=True)
    )
    suggestions = []
    for sps in sps_rows:
        try:
            available = int(getattr(sps, "available_qty", 0))
            alert = int(getattr(sps, "alert_qty", 0) or 0)
        except Exception:
            available, alert = 0, 0
        if alert and int(getattr(sps, "remaining_qty", 0)) <= alert:
            # Skip if already pending in another request
            if sps.product_id in pending_pids:
                continue
            suggestions.append({
                "product_id": sps.product_id,
                "name": sps.product.name,
                "brand": sps.product.brand.name if sps.product.brand_id else "",
                "available": available,
                "suggested": max(alert*2 - int(getattr(sps, "remaining_qty", 0)), alert or 5),
            })

    # Current draft lines to prefill
    # Prefetch availability for draft line products
    draft_qs = draft.lines.select_related("product", "product__brand")
    draft_pids = list(draft_qs.values_list("product_id", flat=True))
    avail_map = {
        sps.product_id: int(getattr(sps, "available_qty", 0))
        for sps in SalesPointStock.objects.filter(salespoint=sp, product_id__in=draft_pids)
    }
    draft_lines = [
        {
            "product_id": ln.product_id,
            "qty": ln.quantity,
            "name": getattr(ln.product, "name", f"#{ln.product_id}"),
            "brand": getattr(getattr(ln.product, "brand", None), "name", ""),
            "available": int(avail_map.get(ln.product_id, 0)),
        }
        for ln in draft_qs
    ]

    return render(request, "sales/manager/manager_restock.html", {
        "salespoint": sp,
        "draft": draft,
        "draft_lines": draft_lines,
        "suggestions": suggestions,
    })


@login_required
def manager_inbound_transfers(request):
    """List inbound restock requests from warehouse to validate reception."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")

    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return render(request, "sales/manager/manager_inbound.html", {"salespoint": None, "rows": [], "msg": "Aucun point de vente lié."})

    # Get restock requests from warehouse - simplified query to avoid SQLite depth issues
    try:
        rows = (
            RestockRequest.objects.select_related("salespoint", "requested_by")
            .filter(salespoint=sp, status__in=['sent', 'partially_validated'])
            .exclude(reference__startswith='WH-RQ-')  # exclude salespoint requests; only inbound from warehouse
            .order_by("-created_at")[:50]
        )
        print(f"DEBUG: Found {len(rows)} restock requests for salespoint {sp}")
        return render(request, "sales/manager/manager_inbound.html", {"salespoint": sp, "rows": rows})
    except Exception as e:
        print(f"DEBUG: Error in manager_inbound_transfers: {e}")
        # Fallback to empty list if query fails
        return render(request, "sales/manager/manager_inbound.html", {"salespoint": sp, "rows": [], "error": str(e)})


@login_required
@require_GET
def api_manager_restock_lines(request, request_id: int):
    """Get lines for a restock request with product details."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Point de vente non défini."}, status=400)
    
    try:
        restock_request = RestockRequest.objects.select_related('salespoint').get(id=request_id, salespoint=sp)
        lines = []
        
        for line in restock_request.lines.select_related('product').filter(validated_at__isnull=True):
            # Handle both new and legacy field names
            quantity = None
            if line.quantity_approved is not None and line.quantity_approved > 0:
                quantity = line.quantity_approved
            elif line.quantity_requested is not None and line.quantity_requested > 0:
                quantity = line.quantity_requested
            elif hasattr(line, 'quantity') and line.quantity > 0:
                quantity = line.quantity  # Legacy field
            
            if quantity:
                # Load brand information safely to avoid SQLite depth issues
                brand_name = None
                try:
                    if hasattr(line.product, 'brand') and line.product.brand:
                        brand_name = line.product.brand.name
                except Exception:
                    pass  # Brand not loaded or error accessing it
                
                lines.append({
                    'id': line.id,
                    'product_id': line.product.id,
                    'product_name': line.product.name,
                    'brand': brand_name,
                    'quantity': quantity,
                    'cost_price': float(line.product.cost_price or 0),
                })
        
        return JsonResponse({
            "ok": True,
            "lines": lines,
            "reference": restock_request.reference or str(restock_request.id),
            "status": restock_request.status
        })
    except RestockRequest.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Demande non trouvée."}, status=404)


@login_required
@require_POST
def api_manager_validate_restock(request, request_id: int):
    """Validate received products from a restock request."""
    print(f"=== VALIDATION REQUEST RECEIVED ===")
    print(f"DEBUG: Request method: {request.method}")
    print(f"DEBUG: Request user: {request.user.username} (role: {getattr(request.user, 'role', 'N/A')})")
    print(f"DEBUG: Request ID: {request_id}")
    print(f"DEBUG: Request headers: {dict(request.headers)}")
    
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        print(f"DEBUG: Access denied - user role: {role}")
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        print(f"DEBUG: No salespoint for user {request.user.username}")
        return JsonResponse({"ok": False, "error": "Point de vente non défini."}, status=400)
    
    print(f"DEBUG: User salespoint: {sp.name if sp else 'None'}")
    
    try:
        import json
        payload = json.loads(request.body.decode('utf-8'))
        validated_lines = payload.get('validated_lines', [])
        
        print(f"DEBUG: Validating restock request {request_id} for salespoint {sp}")
        print(f"DEBUG: Validated lines: {validated_lines}")
        print(f"DEBUG: Request body: {request.body.decode('utf-8')}")
        
        if not validated_lines:
            return JsonResponse({"ok": False, "error": "Aucune ligne sélectionnée pour validation."}, status=400)
        
        restock_request = RestockRequest.objects.select_related('salespoint').get(id=request_id, salespoint=sp)
        print(f"DEBUG: Found restock request: {restock_request.id}, status: {restock_request.status}")
        
        if restock_request.status not in ['sent', 'partially_validated']:
            return JsonResponse({"ok": False, "error": f"Cette demande ne peut plus être modifiée. Statut actuel: {restock_request.status}"}, status=400)
        
        validated_count = 0
        total_value = 0
        
        print(f"DEBUG: Starting validation process...")
        with transaction.atomic():
            # Get warehouse salespoint for stock deduction
            warehouse = SalesPoint.objects.filter(is_warehouse=True).first()
            
            for validated_line in validated_lines:
                line_id = validated_line.get('line_id')
                cost_price = validated_line.get('cost_price', 0)
                

                
                try:
                    line = restock_request.lines.select_related('product').get(id=line_id)

                    
                    # Handle both new and legacy field names
                    quantity = None
                    if line.quantity_approved is not None and line.quantity_approved > 0:
                        quantity = line.quantity_approved

                    elif line.quantity_requested is not None and line.quantity_requested > 0:
                        quantity = line.quantity_requested

                    elif hasattr(line, 'quantity') and line.quantity > 0:
                        quantity = line.quantity  # Legacy field

                    else:
                        # No valid quantity found
                        continue
                    
                    if not quantity or quantity <= 0:

                        continue  # Skip invalid quantities
                    
                    # Capture current stock quantities for audit BEFORE validation
                    sp_stock, _ = SalesPointStock.objects.select_for_update().get_or_create(
                        salespoint=sp,
                        product=line.product,
                        defaults={'opening_qty': 0}
                    )
                    # Calculate stock quantity before validation
                    try:
                        stock_before = int(sp_stock.opening_qty or 0) + int(sp_stock.transfer_in or 0) - int(sp_stock.transfer_out or 0) - int(sp_stock.sold_qty or 0)
                    except Exception:
                        stock_before = int(sp_stock.opening_qty or 0)
                    
                    # Mark line as validated and save stock quantity at validation time
                    line.validated_at = timezone.now()
                    line.stock_qty_at_validation = stock_before
                    line.save(update_fields=['validated_at', 'stock_qty_at_validation'])
                    # Apply inbound at destination
                    SalesPointStock.objects.filter(pk=sp_stock.pk).update(transfer_in=F('transfer_in') + quantity)

                    
                    # On validation: convert in-transit to sold at warehouse
                    if warehouse:
                        wh_stock, _ = SalesPointStock.objects.select_for_update().get_or_create(
                            salespoint=warehouse,
                            product=line.product,
                            defaults={'opening_qty': 0}
                        )
                        # Decrease transfer_out (in-transit) and increase sold_qty
                        SalesPointStock.objects.filter(pk=wh_stock.pk).update(
                            transfer_out=F('transfer_out') - quantity,
                            sold_qty=F('sold_qty') + quantity,
                        )
                        # Optional audit log
                        StockTransaction.objects.create(
                            salespoint=warehouse,
                            product=line.product,
                            qty=0,
                            reason='restock_validated',
                            reference=restock_request.reference or f"REQ{restock_request.id}",
                            user=request.user,
                        )
 
                     
                    # Create stock transaction for destination (positive)
                    StockTransaction.objects.create(
                        salespoint=sp,
                        product=line.product,
                        qty=quantity,
                        reason='restock',
                        reference=restock_request.reference or f"REQ{restock_request.id}",
                        user=request.user,
                    )
 
                    
                    # Create validation audit record
                    try:
                        from apps.inventory.models import RestockValidationAudit
                        # Recompute after (destination)
                        sp_stock.refresh_from_db()
                        try:
                            stock_after = int(sp_stock.opening_qty or 0) + int(sp_stock.transfer_in or 0) - int(sp_stock.transfer_out or 0) - int(sp_stock.sold_qty or 0)
                        except Exception:
                            stock_after = stock_before + quantity
                        RestockValidationAudit.objects.create(
                            restock_request=restock_request,
                            validated_by=request.user,
                            product=line.product,
                            quantity_validated=quantity,
                            stock_before_validation=stock_before,
                            stock_after_validation=stock_after,
                            cost_price_at_validation=line.product.cost_price or 0,
                            total_value=quantity * cost_price,
                        )
                    except Exception as audit_error:
                        # Log error but don't fail the validation
                        pass
                    
                    validated_count += 1
                    total_value += quantity * cost_price

                    
                except RestockLine.DoesNotExist:
                    continue
            
            # Update request status
            total_lines = restock_request.lines.count()
            validated_lines_count = restock_request.lines.filter(validated_at__isnull=False).count()
            

            
            if validated_lines_count == total_lines:
                restock_request.status = 'validated'
                restock_request.validated_at = timezone.now()

            elif validated_lines_count > 0:
                restock_request.status = 'partially_validated'

            else:
                # No lines were validated
                pass
            
            restock_request.save(update_fields=['status', 'validated_at'])

            
            # Send notification to warehouse manager about validation completion
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                from apps.sales.models import Notification
                
                # Find warehouse manager
                warehouse_managers = User.objects.filter(role='warehouse_mgr', is_active=True)
                for manager in warehouse_managers:
                    Notification.objects.create(
                        user=manager,
                        message=f"✅ Approvisionnement validé: {restock_request.reference or f'REQ{restock_request.id}'} - {sp.name}",
                        link=f"/inventory/warehouse/requests/",
                        kind="restock_validated",
                    )

            except Exception as e:
                # Silently handle notification errors
                pass

        return JsonResponse({
            "ok": True,
            "validated_count": validated_count,
            "total_value": total_value,
            "status": restock_request.status
        })
        
    except RestockRequest.DoesNotExist:

        return JsonResponse({"ok": False, "error": "Demande non trouvée."}, status=404)
    except json.JSONDecodeError as e:

        return JsonResponse({"ok": False, "error": "Données invalides."}, status=400)
    except Exception as e:

        return JsonResponse({"ok": False, "error": f"Erreur système: {str(e)}"}, status=500)





@login_required
@require_POST
def api_manager_ack_transfer(request, transfer_id: int):
    """Acknowledge (validate) an inbound transfer and update counters for the destination salespoint."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)

    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente lié."}, status=400)

    with transaction.atomic():
        tr = get_object_or_404(Transfer.objects.select_for_update(), pk=transfer_id)
        if tr.to_salespoint_id != sp.id:
            return JsonResponse({"ok": False, "error": "Ce transfert n'appartient pas à votre point de vente."}, status=403)
        if tr.acknowledged_at:
            return JsonResponse({"ok": True, "already": True})

        tr.acknowledged_at = timezone.now()
        tr.acknowledged_by = request.user
        tr.save(update_fields=["acknowledged_at", "acknowledged_by"])

        # Destination counters: increment transfer_in to reflect physical reception
        try:
            sps = SalesPointStock.objects.select_for_update().get(salespoint=sp, product_id=tr.product_id)
            sps.transfer_in = int(getattr(sps, "transfer_in", 0) or 0) + int(tr.quantity or 0)
            sps.save(update_fields=["transfer_in"])
        except Exception:
            pass

        # Source counters: if the source is the warehouse, only deduct now (on reception)
        try:
            src_sp = getattr(tr, "from_salespoint", None)
            if src_sp and getattr(src_sp, "is_warehouse", False):
                try:
                    sps_src = SalesPointStock.objects.select_for_update().get(salespoint=src_sp, product_id=tr.product_id)
                except SalesPointStock.DoesNotExist:
                    sps_src = SalesPointStock.objects.create(salespoint=src_sp, product_id=tr.product_id)
                sps_src.transfer_out = int(getattr(sps_src, "transfer_out", 0) or 0) + int(tr.quantity or 0)
                sps_src.save(update_fields=["transfer_out"])
        except Exception:
            pass

    return JsonResponse({"ok": True})


@login_required
@require_POST
def api_manager_ack_transfers(request):
    """Bulk acknowledge selected inbound transfers for the manager's salespoint."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)

    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente lié."}, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        ids = payload.get("ids") or []
        ids = [int(x) for x in ids if int(x) > 0]
    except Exception:
        ids = []
    if not ids:
        return JsonResponse({"ok": False, "error": "Aucun transfert sélectionné."}, status=400)

    with transaction.atomic():
        qs = Transfer.objects.select_for_update().filter(id__in=ids, to_salespoint=sp)
        updated = 0
        for tr in qs:
            if tr.acknowledged_at:
                continue
            tr.acknowledged_at = timezone.now()
            tr.acknowledged_by = request.user
            tr.save(update_fields=["acknowledged_at", "acknowledged_by"])
            try:
                sps = SalesPointStock.objects.select_for_update().get(salespoint=sp, product_id=tr.product_id)
                sps.transfer_in = int(getattr(sps, "transfer_in", 0) or 0) + int(tr.quantity or 0)
                sps.save(update_fields=["transfer_in"])
            except Exception:
                pass
            # Source is warehouse: deduct at reception time
            try:
                src_sp = getattr(tr, "from_salespoint", None)
                if src_sp and getattr(src_sp, "is_warehouse", False):
                    try:
                        sps_src = SalesPointStock.objects.select_for_update().get(salespoint=src_sp, product_id=tr.product_id)
                    except SalesPointStock.DoesNotExist:
                        sps_src = SalesPointStock.objects.create(salespoint=src_sp, product_id=tr.product_id)
                    sps_src.transfer_out = int(getattr(sps_src, "transfer_out", 0) or 0) + int(tr.quantity or 0)
                    sps_src.save(update_fields=["transfer_out"])
            except Exception:
                pass
            updated += 1
    return JsonResponse({"ok": True, "updated": updated})


# -----------------------------
# Manager Transfer Request (inter-salespoint)
# -----------------------------

@login_required
def manager_transfer_request(request):
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")
    sp = getattr(request.user, "salespoint", None)
    others = SalesPoint.objects.all()
    if sp:
        others = others.exclude(id=sp.id)
    return render(request, "sales/manager/manager_transfer_request.html", {"salespoint": sp, "others": others})


@login_required
@require_GET
def api_manager_source_stocks(request):
    """Return stock at a selected source salespoint for search/autocomplete."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse([], safe=False)
    try:
        src_id = int(request.GET.get("sp") or 0)
    except Exception:
        src_id = 0
    q = (request.GET.get("q") or "").strip()
    if not src_id:
        return JsonResponse([], safe=False)
    qs = (
        SalesPointStock.objects.filter(salespoint_id=src_id, product__is_active=True)
        .select_related("product", "product__brand")
        .order_by("product__name")
    )
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(product__brand__name__icontains=q))
    data = []
    for sps in qs[:100]:
        data.append({
            "product_id": sps.product_id,
            "name": sps.product.name,
            "brand": sps.product.brand.name if sps.product.brand_id else "",
            "available": int(getattr(sps, "available_qty", 0)),
        })
    return JsonResponse(data, safe=False)


@login_required
@require_POST
def api_manager_save_transfer_request(request):
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente lié."}, status=400)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        from_sp_id = int(payload.get("from_sp") or 0)
        lines = payload.get("lines") or []  # [{product_id, qty}]
        action = (payload.get("action") or "save").lower()
    except Exception:
        return JsonResponse({"ok": False, "error": "Requête invalide."}, status=400)
    if not from_sp_id or (sp and from_sp_id == sp.id):
        return JsonResponse({"ok": False, "error": "Point de vente source invalide."}, status=400)

    with transaction.atomic():
        # Orientation: FROM = source (giving out), TO = requester (current manager's shop)
        req = (
            TransferRequest.objects.filter(from_salespoint_id=from_sp_id, to_salespoint=sp, status="draft")
            .order_by("-created_at").first()
        )
        if not req:
            req = TransferRequest.objects.create(from_salespoint_id=from_sp_id, to_salespoint=sp, requested_by=request.user, status="draft")

        TransferRequestLine.objects.filter(request=req).delete()
        for ln in lines:
            pid = int(ln.get("product_id") or 0)
            # quantity provided by destination later; keep placeholder 1
            qty = 1
            if pid:
                # Snapshot available at source
                sps_src = SalesPointStock.objects.filter(salespoint_id=from_sp_id, product_id=pid).first()
                TransferRequestLine.objects.create(
                    request=req,
                    product_id=pid,
                    quantity=qty,
                    available_at_source=int(getattr(sps_src, "available_qty", 0) if sps_src else 0),
                )
        if action == "send":
            req.status = "sent"
            req.sent_at = timezone.now()
            # Generate per-salespoint daily number when sending (per requester/destination)
            today = timezone.localdate()
            # Lock and compute next sequence (use MAX to avoid gaps and ensure monotonicity)
            agg = (
                TransferRequest.objects
                .select_for_update()
                .filter(to_salespoint=sp, number_date=today)
                .aggregate(m=Max("number_seq"))
            )
            seq = int(agg.get("m") or 0) + 1
            # Prefix: two letters from requesting (destination) salespoint name
            prefix = (sp.name or "SP").strip().upper().replace(" ", "")[:2] if hasattr(sp, "name") else "SP"
            code = f"{prefix}-TRANS-{today.strftime('%d%m%y')}-P-{seq:04d}"
            # Ensure global uniqueness across table (safety loop)
            while TransferRequest.objects.filter(number=code).exists():
                seq += 1
                code = f"{prefix}-TRANS-{today.strftime('%d%m%y')}-P-{seq:04d}"
            req.number = code
            req.number_date = today
            req.number_seq = seq
            req.save(update_fields=["status", "sent_at", "number", "number_date", "number_seq"])
            # Notify source (requested-from) managers about incoming request
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                # Notify managers only (if role field exists); else notify all users at the source salespoint
                try:
                    dest_users = list(User.objects.filter(salespoint_id=from_sp_id, role__in=["sales_manager", "gerant", "gérant"]))
                    if not dest_users:
                        dest_users = list(User.objects.filter(salespoint_id=from_sp_id))
                except Exception:
                    dest_users = list(User.objects.filter(salespoint_id=from_sp_id))
                approver = getattr(request.user, 'username', 'manager')
                msg = f"Transfert approuvé par {approver} • {getattr(req, 'number', '') or f'TR-{req.id}'}"
                for u in dest_users:
                    Notification.objects.create(user=u, message=msg, link="/sales/manager/inbound/")
            except Exception:
                pass
    return JsonResponse({"ok": True, "request_id": req.id, "status": req.status, "number": getattr(req, "number", "")})


@login_required
def manager_transfer_inbox(request):
    """Source manager's inbox for requests sent to this salespoint to fulfill (approve/reject)."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return render(request, "sales/manager/manager_transfer_inbox.html", {"salespoint": None, "rows": [], "msg": "Aucun point de vente lié."})

    status = (request.GET.get("status") or "").strip() or "sent"
    qs = (
        TransferRequest.objects.select_related("from_salespoint", "to_salespoint", "requested_by")
        .filter(from_salespoint=sp)
        .order_by("-created_at")
    )
    if status in {"draft", "sent", "approved", "rejected", "fulfilled", "cancelled"}:
        qs = qs.filter(status=status)

    return render(request, "sales/manager/manager_transfer_inbox.html", {"salespoint": sp, "rows": qs[:500], "status": status})


@login_required
def manager_transfer_history(request):
    """List both incoming and outgoing transfer requests affecting the manager's salespoint."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return render(request, "sales/manager/manager_transfer_history.html", {"salespoint": None, "rows": []})

    kind = (request.GET.get("kind") or "").strip()  # 'in' | 'out' | ''
    q = (request.GET.get("q") or "").strip()
    start = request.GET.get("start") or ""
    end = request.GET.get("end") or ""

    rows = (
        TransferRequest.objects.select_related("from_salespoint", "to_salespoint")
        .filter(Q(from_salespoint=sp) | Q(to_salespoint=sp))
        .order_by("-created_at")
    )
    if kind == "in":
        rows = rows.filter(to_salespoint=sp)
    elif kind == "out":
        rows = rows.filter(from_salespoint=sp)
    if q:
        rows = rows.filter(Q(number__icontains=q) | Q(from_salespoint__name__icontains=q) | Q(to_salespoint__name__icontains=q))
    try:
        if start:
            rows = rows.filter(created_at__date__gte=parse_date(start))
        if end:
            rows = rows.filter(created_at__date__lte=parse_date(end))
    except Exception:
        pass

    return render(request, "sales/manager/manager_transfer_history.html", {"salespoint": sp, "rows": rows[:500], "kind": kind, "q": q, "start": start, "end": end})


@login_required
def manager_transfer_drafts(request):
    """List current manager's transfer requests in draft state (brouillons)."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return redirect("sales:dashboard")
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return render(request, "sales/manager/manager_transfer_drafts.html", {"salespoint": None, "rows": [], "msg": "Aucun point de vente lié."})
    qs = (
        TransferRequest.objects.select_related("from_salespoint", "to_salespoint", "requested_by")
        .filter(from_salespoint=sp, status="draft")
        .order_by("-created_at")
    )
    return render(request, "sales/manager/manager_transfer_drafts.html", {"salespoint": sp, "rows": qs[:500]})


@login_required
@require_GET
def api_manager_tr_lines(request, req_id: int):
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    req = get_object_or_404(TransferRequest.objects.select_related("from_salespoint", "to_salespoint").prefetch_related("lines__product", "lines__product__brand"), pk=req_id)
    sp = getattr(request.user, "salespoint", None)
    if sp and (req.from_salespoint_id != sp.id and req.to_salespoint_id != sp.id):
        return JsonResponse({"ok": False, "error": "Demande non adressée à votre point de vente pour consultation."}, status=403)
    data = {
        "ok": True,
        "id": req.id,
        "number": getattr(req, "number", "") or f"{(req.from_salespoint.name or 'SP')[:2].upper()}-TRANS-{req.created_at.strftime('%d%m%y')}-P-{req.id:04d}",
        "from": req.from_salespoint.name,
        "to": req.to_salespoint.name,
        "status": req.status,
        "lines": [
            {
                "product_id": ln.product_id,
                "name": getattr(ln.product, "name", f"#{ln.product_id}"),
                "brand": getattr(getattr(ln.product, "brand", None), "name", ""),
                "qty": int(ln.quantity or 0),
                "available_at_source": int(getattr(ln, "available_at_source", 0) or 0),
            }
            for ln in req.lines.all()
        ],
    }
    return JsonResponse(data)


@login_required
@require_POST
def api_manager_tr_allocate(request):
    """Reserve a per-salespoint daily number for an in-progress transfer request.
    Idempotent for the same draft (from_sp, to_sp, today): reuses existing draft if any.
    """
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente lié."}, status=400)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        to_sp = int(payload.get("to_sp") or 0)
    except Exception:
        return JsonResponse({"ok": False, "error": "Requête invalide."}, status=400)
    if not to_sp or to_sp == sp.id:
        return JsonResponse({"ok": False, "error": "Point de vente invalide."}, status=400)

    today = timezone.localdate()
    # no draft creation; just compute preview number
    seq = (
        TransferRequest.objects.select_for_update()
        .filter(from_salespoint=sp, number_date=today)
        .aggregate(c=Count("id")).get("c", 0)
    ) or 0
    seq += 1
    prefix = (sp.name or "SP").strip().upper().replace(" ", "")[:2] if hasattr(sp, "name") else "SP"
    code = f"{prefix}-TRANS-{today.strftime('%d%m%y')}-P-{seq:04d}"
    return JsonResponse({"ok": True, "request_id": 0, "number": code})


@login_required
@require_POST
def api_manager_tr_decide(request, req_id: int):
    """Approve or reject a transfer request. On approve, create Transfer rows and update source counters."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente lié."}, status=400)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        decision = (payload.get("decision") or "").lower()  # 'approve' | 'reject'
        adj_lines = payload.get("lines") or []  # [{product_id, qty}] when approving
    except Exception:
        decision = ""
        adj_lines = []
    if decision not in {"approve", "reject"}:
        return JsonResponse({"ok": False, "error": "Décision invalide."}, status=400)

    with transaction.atomic():
        req = get_object_or_404(TransferRequest.objects.select_for_update().prefetch_related("lines"), pk=req_id)
        # Only the SOURCE (giver) salespoint is allowed to approve/reject
        if req.from_salespoint_id != sp.id:
            return JsonResponse({"ok": False, "error": "Seul le point de vente source peut approuver cette demande."}, status=403)
        if req.status not in {"sent"}:
            return JsonResponse({"ok": False, "error": "Statut incompatible."}, status=400)

        if decision == "reject":
            req.status = "rejected"
            req.save(update_fields=["status"])
            return JsonResponse({"ok": True, "status": req.status})

        # Approve: create Transfers with manager-assigned quantities
        qty_map = {int(x.get("product_id") or 0): int(x.get("qty") or 0) for x in adj_lines}
        created_ids = []
        touched_product_ids = set()
        for ln in req.lines.all():
            send_qty = qty_map.get(ln.product_id, 0)
            if send_qty <= 0:
                continue
            # Validate against available_at_source snapshot (best-effort)
            max_src = int(getattr(ln, "available_at_source", 0) or 0)
            if send_qty > max_src and max_src > 0:
                send_qty = max_src
            # Persist approved quantity on the request line for history
            try:
                if int(getattr(ln, "quantity", 0)) != int(send_qty):
                    ln.quantity = int(send_qty)
                    ln.save(update_fields=["quantity"])
            except Exception:
                pass
            tr = Transfer.objects.create(
                product_id=ln.product_id,
                from_salespoint_id=req.from_salespoint_id,
                to_salespoint_id=req.to_salespoint_id,
                quantity=send_qty,
            )
            created_ids.append(tr.id)
            touched_product_ids.add(ln.product_id)
            # Update source counters immediately
            try:
                sps_src = SalesPointStock.objects.select_for_update().get(salespoint_id=req.from_salespoint_id, product_id=ln.product_id)
                sps_src.transfer_out = int(getattr(sps_src, "transfer_out", 0) or 0) + int(send_qty or 0)
                sps_src.save(update_fields=["transfer_out"])
            except Exception:
                pass
            # Immediately credit destination so it's sellable right after approval
            try:
                sps_dst = SalesPointStock.objects.select_for_update().get(salespoint_id=req.to_salespoint_id, product_id=ln.product_id)
            except SalesPointStock.DoesNotExist:
                sps_dst = SalesPointStock.objects.create(salespoint_id=req.to_salespoint_id, product_id=ln.product_id)
            try:
                sps_dst.transfer_in = int(getattr(sps_dst, "transfer_in", 0) or 0) + int(send_qty or 0)
                sps_dst.save(update_fields=["transfer_in"])
            except Exception:
                pass
            # Mark transfer as acknowledged for destination so it won't be double-counted later
            try:
                tr.acknowledged_at = timezone.now()
                tr.acknowledged_by = None
                tr.save(update_fields=["acknowledged_at", "acknowledged_by"])
            except Exception:
                pass

        req.status = "approved"
        try:
            req.approved_at = timezone.now()
            req.approved_by = request.user
            req.save(update_fields=["status","approved_at","approved_by"])
        except Exception:
            req.save(update_fields=["status"])
        # Recompute any denormalized counters used by stock listings (both source and destination)
        try:
            if touched_product_ids:
                _update_salespoint_stock_denorm(req.from_salespoint, list(touched_product_ids))
                _update_salespoint_stock_denorm(req.to_salespoint, list(touched_product_ids))
        except Exception:
            pass
        # Notify source salespoint via a lightweight flag the sender UI can poll
        try:
            request.session["last_transfer_update"] = timezone.now().isoformat()
        except Exception:
            pass
        # Create notifications: one for requester (approved), one optional for provider (outgoing)
        try:
            # Notify requester (manager of from_salespoint). We assume there's at least one manager user linked; else notify the current user if same sp.
            to_notify = []
            try:
                # If User model has salespoint and role, notify managers of from_salespoint
                from django.contrib.auth import get_user_model
                User = get_user_model()
                to_notify = list(User.objects.filter(salespoint_id=req.from_salespoint_id))
            except Exception:
                to_notify = [request.user]
            approver = getattr(request.user, 'username', 'manager')
            msg = f"Transfert approuvé par {approver} • {getattr(req, 'number', '') or f'TR-{req.id}'}"
            for u in to_notify:
                Notification.objects.create(user=u, message=msg, link="/sales/manager/inbound/")
        except Exception:
            pass
        return JsonResponse({"ok": True, "status": req.status, "transfers": created_ids})


@login_required
@require_POST
def api_manager_tr_send(request, req_id: int):
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente lié."}, status=400)
    with transaction.atomic():
        req = get_object_or_404(TransferRequest.objects.select_for_update(), pk=req_id)
        if req.from_salespoint_id != sp.id:
            return JsonResponse({"ok": False, "error": "Ce brouillon n'appartient pas à votre point de vente."}, status=403)
        if req.status != "draft":
            return JsonResponse({"ok": False, "error": "Ce brouillon n'est pas à l'état 'draft'."}, status=400)
        req.status = "sent"
        req.sent_at = timezone.now()
        # allocate number if missing
        if not getattr(req, "number", ""):
            today = timezone.localdate()
            seq = (
                TransferRequest.objects.select_for_update()
                .filter(from_salespoint=sp, number_date=today)
                .aggregate(c=Count("id")).get("c", 0)
            ) or 0
            seq += 1
            prefix = (sp.name or "SP").strip().upper().replace(" ", "")[:2] if hasattr(sp, "name") else "SP"
            req.number = f"{prefix}-TRANS-{today.strftime('%d%m%y')}-P-{seq:04d}"
            req.number_date = today
            req.number_seq = seq
        req.save(update_fields=["status", "sent_at", "number", "number_date", "number_seq"])
    return JsonResponse({"ok": True, "status": req.status, "number": req.number})


@login_required
@require_GET
def api_manager_transfer_updates(request):
    """Simple poll endpoint to let the sender know when a request was approved/rejected.
    For now, return last approved/rejected requests initiated by the current manager's salespoint.
    """
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse({"ok": False}, status=403)
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": True, "rows": []})
    qs = (
        TransferRequest.objects.filter(from_salespoint=sp, status__in=["approved", "rejected"])  # recently decided
        .order_by("-updated_at")[:10]
    )
    rows = [
        {"id": r.id, "number": getattr(r, "number", "") or f"{(r.from_salespoint.name or 'SP')[:2].upper()}-TRANS-{r.created_at.strftime('%d%m%y')}-P-{r.id:04d}", "status": r.status, "to": r.to_salespoint.name}
        for r in qs
    ]
    return JsonResponse({"ok": True, "rows": rows})


@login_required
@require_GET
def api_notifications(request):
    """Return unread notifications for current user, grouped to avoid duplicates.

    Groups by (message, link, kind) and returns:
      - ids: list of notification ids in the group
      - msg: message
      - link: optional link
      - kind: optional kind
      - count: number of notifications in the group
      - created: timestamp of the most recent notification in the group (dd/mm HH:MM)
    """
    qs = Notification.objects.filter(user=request.user, read_at__isnull=True).order_by("-created_at")[:200]
    groups: dict[tuple[str, str, str], dict] = {}
    for n in qs:
        key = (n.message or "", n.link or "", n.kind or "")
        g = groups.get(key)
        if not g:
            groups[key] = {
                "ids": [n.id],
                "msg": n.message,
                "link": n.link,
                "kind": n.kind or "info",
                "count": 1,
                "created": n.created_at,
            }
        else:
            g["ids"].append(n.id)
            g["count"] += 1
            if n.created_at and (g["created"] is None or n.created_at > g["created"]):
                g["created"] = n.created_at

    rows = []
    for g in groups.values():
        rows.append({
            "ids": g["ids"],
            "msg": g["msg"],
            "link": g["link"],
            "kind": g["kind"],
            "count": g["count"],
            "created": (g["created"].strftime("%d/%m %H:%M") if g["created"] else ""),
        })
    # Sort by most recent created
    rows.sort(key=lambda r: r.get("created", ""), reverse=True)
    return JsonResponse({"ok": True, "rows": rows})


@login_required
@require_POST
def api_notifications_mark_read(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        ids = [int(x) for x in (payload.get("ids") or []) if int(x) > 0]
    except Exception:
        ids = []
    if not ids:
        return JsonResponse({"ok": True})
    Notification.objects.filter(user=request.user, id__in=ids, read_at__isnull=True).update(read_at=timezone.now())
    return JsonResponse({"ok": True})


@login_required
@require_GET
def api_manager_restock_search(request):
    """Search products for the manager restock page (excluding already listed)."""
    role = getattr(request.user, "role", "")
    if not (request.user.is_superuser or _is_manager_role(role)):
        return JsonResponse([], safe=False)
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse([], safe=False)
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse([], safe=False)

    # Exclude products already present in current draft
    draft = RestockRequest.objects.filter(salespoint=sp, status="draft").order_by("-created_at").first()
    exclude_ids = set()
    if draft:
        exclude_ids = set(draft.lines.values_list("product_id", flat=True))

    qs = (
        SalesPointStock.objects
        .filter(salespoint=sp, product__is_active=True)
        .select_related("product", "product__brand")
        .order_by("product__name")
    )
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(product__brand__name__icontains=q))
    if exclude_ids:
        qs = qs.exclude(product_id__in=list(exclude_ids))

    data = []
    for sps in qs[:50]:
        data.append({
            "product_id": sps.product_id,
            "name": sps.product.name,
            "brand": sps.product.brand.name if sps.product.brand_id else "",
            "available": int(getattr(sps, "available_qty", 0)),
        })
    return JsonResponse(data, safe=False)

@login_required
@require_GET
def api_cashier_pending(request):
    if not _is_cashier(request.user):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    sp = getattr(request.user, "salespoint", None)
    qs = (
        Sale.objects.filter(status="awaiting_cashier")
        .select_related("seller", "salespoint")
        .order_by("-created_at")
    )
    if sp:
        qs = qs.filter(salespoint=sp)
    data = [
        {
            "id": s.id,
            "number": s.number,
            "customer": s.customer_name,
            "seller": s.seller.get_full_name() or s.seller.username,
            "total": str(s.total_amount),
            "created": s.created_at.strftime("%d/%m/%Y %H:%M"),
        }
        for s in qs[:500]
    ]
    return JsonResponse(data, safe=False)


# New endpoint: Sales summary for cashier's salespoint on a given date
@login_required
@require_GET
def api_cashier_sales_summary(request):
    """Return count and sum of sales for the current cashier's salespoint on a given date.
    Query parameter: ?d=YYYY-MM-DD
    """
    if not _is_cashier(request.user):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)

    d_str = request.GET.get("d") or ""
    the_date = parse_date(d_str) or timezone.localdate()

    sp = getattr(request.user, "salespoint", None)
    qs = Sale.objects.filter(created_at__date=the_date, status="approved")
    if sp:
        qs = qs.filter(salespoint=sp)

    data = qs.aggregate(c=Count("id"), t=Sum("total_amount"))
    return JsonResponse({
        "count": int(data.get("c") or 0),
        "total": int(data.get("t") or 0),
    })

@login_required
@require_GET
def api_cashier_sale_detail(request, sale_id: int):
    if not _is_cashier(request.user):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    s = get_object_or_404(Sale.objects.select_related("seller", "salespoint"), pk=sale_id)
    sp = getattr(request.user, "salespoint", None)
    if sp and (not request.user.is_superuser) and s.salespoint_id != sp.id:
        return JsonResponse({"ok": False, "error": "Cette vente n'appartient pas à votre point de vente."}, status=403)
    items = [
        {
            "name": li.product.name,
            "qty": li.quantity,
            "unit": str(li.unit_price),
            "total": str(li.line_total),
        }
        for li in s.items.select_related("product")
    ]
    return JsonResponse(
        {
            "id": s.id,
            "number": s.number,
            "customer": s.customer_name,
            "phone": s.customer_phone,
            "seller": s.seller.get_full_name() or s.seller.username,
            "total": str(s.total_amount),
            "items": items,
            # Expose extended details when present (all optional)
            "customer_details": getattr(s, "customer_details", None),
            "chassis_number": getattr(s, "chassis_number", ""),
            "engine_number": getattr(s, "engine_number", ""),
            "amount_in_words": getattr(s, "amount_in_words", ""),
        }
    )

@login_required
@require_POST
def api_cashier_validate(request, sale_id: int):
    if not _is_cashier(request.user):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    try:
        amount_str = request.POST.get("amount") or "0"
        amt = Decimal(str(amount_str))
        with transaction.atomic():
            sale = get_object_or_404(Sale.objects.select_for_update(), pk=sale_id)
            if sale.status != "awaiting_cashier":
                return JsonResponse({"ok": False, "error": "Cette vente n'est pas en attente de caisse."}, status=400)
            sp = getattr(request.user, "salespoint", None)
            if sp and (not request.user.is_superuser) and sale.salespoint_id != sp.id:
                return JsonResponse({"ok": False, "error": "Cette vente n'appartient pas à votre point de vente."}, status=403)
            total = sale.total_amount or Decimal("0")
            # Enforce sufficient cash for immediate payments
            if (getattr(sale, "payment_type", "cash") == "cash") and (amt < total):
                return JsonResponse({"ok": False, "error": "Montant reçu insuffisant."}, status=400)
            # Try to use domain service if present (handles reservations/stock), else fallback
            try:
                from .services import approve_sale as _approve_sale  # optional import
                res = _approve_sale(sale=sale, amount_received=amt, cashier=request.user)
                change = None
                if isinstance(res, dict):
                    change = Decimal(str(res.get("change", amt - total)))
                if change is None:
                    change = amt - total
            except Exception:
                # Fallback: robust update with domain method or direct fields
                change = amt - total
                try:
                    # Prefer domain method if present
                    if hasattr(sale, "approve") and callable(getattr(sale, "approve")):
                        sale.approve(cashier=request.user, received_amount=amt, save=True)
                    else:
                        from django.utils import timezone as _tz
                        if hasattr(sale, "cashier"):
                            sale.cashier = request.user
                        if hasattr(sale, "approved_at"):
                            sale.approved_at = _tz.now()
                        if hasattr(sale, "received_amount"):
                            sale.received_amount = amt
                        sale.status = "approved"
                        sale.save(update_fields=[f for f in ["status", "cashier", "approved_at", "received_amount"] if hasattr(sale, f)])
                except Exception:
                    # Last resort: at least mark approved
                    sale.status = "approved"
                    sale.save(update_fields=["status"])

            # Persist stock decrement at the salespoint (source of truth)
            try:
                # Use model batch helper: moves reserved -> sold for all items in this sale
                SalesPointStock.commit_for_sale(sale)
            except Exception:
                # Non-fatal: do not block approval on stock decrement errors
                pass
            # Keep SalesPointStock admin numbers in sync (if denorm fields exist)
            try:
                affected_products = list(sale.items.values_list("product_id", flat=True))
                _update_salespoint_stock_denorm(sale.salespoint, affected_products)
            except Exception:
                # Non-fatal: do not block approval if denorm sync fails
                pass
        receipt_url = reverse("sales:sales_print_receipt", args=[sale_id])
        return JsonResponse({"ok": True, "change": str(change), "receipt_url": receipt_url})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e) or "Erreur de validation."}, status=400)

@login_required
@require_POST
def api_cashier_cancel(request, sale_id: int):
    if not _is_cashier(request.user):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)
    try:
        with transaction.atomic():
            sale = get_object_or_404(Sale.objects.select_for_update(), pk=sale_id)
            if sale.status not in ("awaiting_cashier", "approved", "draft"):
                return JsonResponse({"ok": False, "error": "Statut de vente invalide pour annulation."}, status=400)
            sp = getattr(request.user, "salespoint", None)
            if sp and (not request.user.is_superuser) and sale.salespoint_id != sp.id:
                return JsonResponse({"ok": False, "error": "Cette vente n'appartient pas à votre point de vente."}, status=403)
            try:
                from .services import cancel_sale as _cancel_sale  # optional import
                _cancel_sale(sale=sale)
            except Exception:
                # Fallback: release reservations then mark as cancelled
                try:
                    SalesPointStock.release_for_sale(sale)
                except Exception:
                    pass
                sale.status = "cancelled"
                sale.save(update_fields=["status"])
            # Keep SalesPointStock admin numbers in sync after cancellation
            try:
                affected_products = list(sale.items.values_list("product_id", flat=True))
                _update_salespoint_stock_denorm(sale.salespoint, affected_products)
            except Exception:
                # Non-fatal: do not block cancellation if denorm sync fails
                pass
        return JsonResponse({"ok": True})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e) or "Erreur d'annulation."}, status=400)


# Inserted view for cashier_report
@login_required
def cashier_report(request):
    """
    Cashier daily report page:
    - GET: show form (date defaults to today) + table of past reports by this cashier.
    - POST: create (or update if still pending) a report row for the chosen date.
    """
    if not _is_cashier(request.user):
        return redirect("sales:dashboard")

    user = request.user
    sp = getattr(user, "salespoint", None)
    if sp is None:
        messages.error(request, "Aucun point de vente n'est associé à votre compte.")
        return redirect("sales:cashier_dashboard")

    # Selected date: from POST > GET > today
    selected_date_str = request.POST.get("report_date") or request.GET.get("report_date")
    if selected_date_str:
        try:
            y, m, d = map(int, selected_date_str.split("-"))
            selected_date = date(y, m, d)
        except Exception:
            selected_date = timezone.localdate()
    else:
        selected_date = timezone.localdate()

    if request.method == "POST":
        amount_raw = (request.POST.get("total_amount") or "").replace(" ", "").replace(",", "")
        try:
            total_amount = int(amount_raw)
            if total_amount < 0:
                raise ValueError
        except ValueError:
            messages.error(request, "Montant invalide.")
            return redirect("sales:cashier_report")

        # Server-side guard: ensure system has sales for that date at this salespoint
        sys_qs = Sale.objects.filter(created_at__date=selected_date)
        if sp:
            sys_qs = sys_qs.filter(salespoint=sp)
        sys_agg = sys_qs.aggregate(c=Count("id"), t=Sum("total_amount"))
        if not sys_agg.get("c"):
            messages.error(request, "Aucune vente enregistrée dans le système pour cette date.")
            return redirect("sales:cashier_report")

        try:
            with transaction.atomic():
                obj, created = CashierDailyReport.objects.get_or_create(
                    cashier=user,
                    salespoint=sp,
                    report_date=selected_date,
                    defaults={"total_amount": total_amount, "status": "pending"},
                )
                if not created:
                    if obj.status == "pending":
                        obj.total_amount = total_amount
                        obj.save(update_fields=["total_amount"])
                    else:
                        messages.warning(request, "Ce rapport a déjà été traité par la comptabilité.")
                        return redirect("sales:cashier_report")
        except IntegrityError:
            messages.error(request, "Impossible d'enregistrer le rapport. Réessayez.")
            return redirect("sales:cashier_report")

        messages.success(
            request,
            f"Rapport enregistré pour le {selected_date.strftime('%d/%m/%Y')} : {total_amount:,}".replace(",", " ") + " FCFA."
        )
        return redirect("sales:cashier_report")

    # ---- Filters for previous reports (server-side) ----
    f_from_str = (request.GET.get("from") or "").strip()
    f_to_str   = (request.GET.get("to") or "").strip()
    f_status   = (request.GET.get("status") or "").strip()

    df = parse_date(f_from_str) if f_from_str else None
    dt = parse_date(f_to_str) if f_to_str else None

    base_rows = CashierDailyReport.objects.filter(cashier=user, salespoint=sp)
    if df:
        base_rows = base_rows.filter(report_date__gte=df)
    if dt:
        base_rows = base_rows.filter(report_date__lte=dt)
    if f_status in {"pending", "approved", "rejected"}:
        base_rows = base_rows.filter(status=f_status)

    rows = base_rows.order_by("-report_date", "-created_at")[:500]

    context = {
        "salespoint": sp,
        "selected_date": selected_date,
        "rows": rows,
        # prefill filter inputs
        "f_from": f_from_str if df else "",
        "f_to": f_to_str if dt else "",
        "f_status": f_status if f_status in {"pending", "approved", "rejected"} else "",
    }
    return render(request, "sales/cashier/cashier_report.html", context)

@login_required
@require_GET
def print_receipt(request, sale_id: int):
    sale = get_object_or_404(
        Sale.objects.select_related("seller", "salespoint").prefetch_related("items__product"),
        pk=sale_id
    )
    return render(request, "sales/cashier/receipt.html", {"sale": sale})


# -----------------------------
# Cancellation workflow endpoints
# -----------------------------

@login_required
@require_GET
def api_find_sale_by_number(request):
    sp = getattr(request.user, "salespoint", None)
    if not sp:
        return JsonResponse({"ok": False, "error": "Aucun point de vente."}, status=400)
    number = (request.GET.get("q") or "").strip()
    if not number:
        return JsonResponse({"ok": False, "error": "Numéro de reçu requis."}, status=400)
    try:
        sale = find_sale_by_number(salespoint=sp, number=number)
        # Guard: only validated (approved) sales are eligible for the cancellation flow
        if getattr(sale, "status", "") != "approved":
            return JsonResponse(
                {"ok": False, "error": "Ce reçu n'a pas été validé à la caisse. Seules les ventes validées peuvent être annulées."},
                status=400,
            )
        items = [
            {
                "id": it.id,
                "product_id": it.product_id,
                "product": getattr(it.product, "name", f"#{it.product_id}"),
                "qty": int(it.quantity),
                "unit_price": int(it.unit_price),
                "line_total": int(it.line_total),
            }
            for it in sale.items.select_related("product")
        ]
        # Consider the approval date when available for same-day cancellation
        try:
            approved_date = sale.approved_at.date() if getattr(sale, "approved_at", None) else None
        except Exception:
            approved_date = None
        base_date = approved_date or sale.created_at.date()
        is_today = base_date == timezone.localdate()
        # Seller display name
        seller_name = getattr(sale.seller, "get_full_name", lambda: "")() or getattr(sale.seller, "username", "")

        # Extended (all optional; front-end is tolerant)
        payload = {
            "ok": True,
            "sale_id": sale.id,
            "number": sale.number,
            "date": sale.created_at.strftime("%Y-%m-%d"),
            "status": sale.status,
            "is_today": is_today,
            "total": int(sale.total_amount),
            "items": items,
            # New fields for UI completeness
            "customer_name": getattr(sale, "customer_name", "") or "",
            "customer_phone": getattr(sale, "customer_phone", "") or "",
            "payment_type": getattr(sale, "payment_type", "") or "",
            "kind": getattr(sale, "kind", "") or "",
            "seller": seller_name,
            # Moto / extended
            "customer_details": getattr(sale, "customer_details", None),
            "chassis_number": getattr(sale, "chassis_number", "") or "",
            "engine_number": getattr(sale, "engine_number", "") or "",
            "amount_in_words": getattr(sale, "amount_in_words", "") or "",
        }
        return JsonResponse(payload)
    except SaleError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=404)


@login_required
@require_POST
def api_cancel_sale_immediate(request):
    """Immediate (same-day) cancellation with stock restore and totals update.
    Body accepts either:
    - {"sale_id": <int>, "lines": [{"sale_item_id": <id>, "quantity": <n>}, ...]}
    - or {"sale_id": <int>, "item_quantities": {"<sale_item_id>": <n>, ...}}
    If no lines provided, cancels all items.
    """
    try:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            return JsonResponse({"ok": False, "error": "Requête invalide."}, status=400)

        reason = (data.get("reason") or data.get("motif") or "").strip()
        if not reason:
            return JsonResponse({"ok": False, "error": "Motif d'annulation requis."}, status=400)

        sale_id = int(data.get("sale_id") or 0)
        if not sale_id:
            return JsonResponse({"ok": False, "error": "Identifiant de vente manquant."}, status=400)

        # Normalize items payload
        item_quantities = data.get("item_quantities") or {}
        if not item_quantities:
            lines = data.get("lines") or []
            if isinstance(lines, list) and lines:
                tmp = {}
                for ln in lines:
                    sid = int(ln.get("sale_item_id") or 0)
                    qty = int(ln.get("quantity") or 0)
                    if sid and qty:
                        tmp[sid] = qty
                item_quantities = tmp

        with transaction.atomic():
            sale = get_object_or_404(Sale.objects.select_for_update(), pk=sale_id)
            # Ensure same salespoint unless superuser
            sp = getattr(request.user, "salespoint", None)
            if sp and (not request.user.is_superuser) and sale.salespoint_id != sp.id:
                return JsonResponse({"ok": False, "error": "Cette vente n'appartient pas à votre point de vente."}, status=403)
            # Guard: only validated (approved) sales can be cancelled
            if getattr(sale, "status", "") != "approved":
                return JsonResponse(
                    {"ok": False, "error": "Cette vente n'a pas été validée à la caisse. Seules les ventes validées peuvent être annulées."},
                    status=400,
                )

            try:
                try:
                    sale = cancel_sale_same_day(sale=sale, item_quantities=item_quantities or None, actor=request.user, reason=reason)
                except TypeError:
                    # older signature without reason
                    sale = cancel_sale_same_day(sale=sale, item_quantities=item_quantities or None, actor=request.user)
                    # best-effort: persist reason on the Sale if field exists
                    try:
                        if hasattr(sale, "cancellation_reason"):
                            sale.cancellation_reason = reason
                            sale.save(update_fields=["cancellation_reason"])  # ignore if field missing
                    except Exception:
                        pass
            except SaleError as e:
                return JsonResponse({"ok": False, "error": str(e)}, status=400)

        return JsonResponse({
            "ok": True,
            "sale_id": sale.id,
            "status": sale.status,
            "total": int(sale.total_amount),
        })
    except Exception as e:
        # Catch-all to avoid 500 and surface a readable error on the UI
        return JsonResponse({"ok": False, "error": str(e) or "Erreur serveur."}, status=500)


@login_required
@require_POST
def api_cancel_sale_request(request):
    """Create a pending cancellation request (for non-same-day sales)."""
    try:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            return JsonResponse({"ok": False, "error": "Requête invalide."}, status=400)

        reason = (data.get("reason") or data.get("motif") or "").strip()
        if not reason:
            return JsonResponse({"ok": False, "error": "Motif d'annulation requis."}, status=400)

        sale_id = int(data.get("sale_id") or 0)
        if not sale_id:
            return JsonResponse({"ok": False, "error": "Identifiant de vente manquant."}, status=400)

        # Normalize to dict of {sale_item_id: qty}
        item_quantities = data.get("item_quantities") or {}
        if not item_quantities:
            lines = data.get("lines") or []
            if isinstance(lines, list) and lines:
                tmp = {}
                for ln in lines:
                    sid = int(ln.get("sale_item_id") or 0)
                    qty = int(ln.get("quantity") or 0)
                    if sid and qty:
                        tmp[sid] = qty
                item_quantities = tmp

        with transaction.atomic():
            sale = get_object_or_404(Sale.objects.select_for_update(), pk=sale_id)
            sp = getattr(request.user, "salespoint", None)
            if sp and (not request.user.is_superuser) and sale.salespoint_id != sp.id:
                return JsonResponse({"ok": False, "error": "Cette vente n'appartient pas à votre point de vente."}, status=403)

            # Guard: only validated (approved) sales can be cancelled
            if getattr(sale, "status", "") != "approved":
                return JsonResponse(
                    {"ok": False, "error": "Cette vente n'a pas été validée à la caisse. Seules les ventes validées peuvent être annulées."},
                    status=400,
                )

            try:
                try:
                    req = create_cancellation_request(sale=sale, item_quantities=item_quantities or None, requested_by=request.user, reason=reason)
                except TypeError:
                    # older signature without reason
                    req = create_cancellation_request(sale=sale, item_quantities=item_quantities or None, requested_by=request.user)
                    # best-effort: set reason on the model if the service didn't
                    try:
                        if hasattr(req, "reason") and not getattr(req, "reason", None):
                            req.reason = reason
                            req.save(update_fields=["reason"])  # ignore if field missing
                    except Exception:
                        pass
            except SaleError as e:
                return JsonResponse({"ok": False, "error": str(e)}, status=400)

        return JsonResponse({"ok": True, "request_id": req.id})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e) or "Erreur serveur."}, status=500)


@login_required
@require_POST
def api_accounting_approve_cancellation(request):
    """Approve a pending cancellation request (accounting)."""
    user = request.user
    if not (user.is_superuser or getattr(user, "is_staff", False) or getattr(user, "role", "") in {"accountant", "commercial_director"}):
        return JsonResponse({"ok": False, "error": "Accès refusé."}, status=403)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Requête invalide."}, status=400)

    req_id = int(data.get("request_id") or 0)
    if not req_id:
        return JsonResponse({"ok": False, "error": "Identifiant de demande manquant."}, status=400)

    from .models import CancellationRequest
    with transaction.atomic():
        req = get_object_or_404(CancellationRequest.objects.select_for_update(), pk=req_id)
        try:
            approve_cancellation_request(request=req, approver=user)
        except SaleError as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)

    return JsonResponse({"ok": True, "sale_id": req.sale_id, "status": req.status})


@login_required
def commercial_dashboard(request):
    """Commercial Director Dashboard - Product management and warehouse restocking."""
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return redirect('sales:dashboard')

    # Get all products with their details, with optional search
    q = (request.GET.get('q') or '').strip()
    ptype = (request.GET.get('type') or '').strip().lower()
    products = Product.objects.filter(is_active=True).select_related('brand').order_by('name')
    if ptype in {'moto','piece'}:
        products = products.filter(product_type=ptype)
    if q:
        qt = q.lower()
        type_q = None
        if qt in {'moto', 'motocycle', 'motorcycle', 'motos'}:
            type_q = 'moto'
        elif qt in {'piece', 'pièce', 'pieces', 'pièces'}:
            type_q = 'piece'
        cond = Q(name__icontains=q) | Q(brand__name__icontains=q)
        if type_q:
            cond = cond | Q(product_type=type_q)
        products = products.filter(cond)
    
    # Pagination
    paginator = Paginator(products, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Get warehouse for restocking
    warehouse = SalesPoint.objects.filter(is_warehouse=True).first()
    
    # Support partial rendering for AJAX live search
    if (request.GET.get('partial') or '').strip().lower() == 'table' or request.headers.get('x-requested-with') == 'XMLHttpRequest':
        # For export, return all products without pagination
        if request.GET.get('export') == 'all':
            all_products = Product.objects.filter(is_active=True).select_related('brand').order_by('name')
            if q:
                if q.lower() == 'moto':
                    all_products = all_products.filter(product_type='moto')
                elif q.lower() in ('pièce', 'piece', 'piÃ¨ce'):
                    all_products = all_products.filter(product_type='piece')
                else:
                    all_products = all_products.filter(Q(name__icontains=q) | Q(brand__name__icontains=q))
            if product_type_filter:
                all_products = all_products.filter(product_type=product_type_filter)
            
            html = render(request, 'sales/commercial/_table_only.html', {
                'products': all_products,
            }).content
        else:
            html = render(request, 'sales/commercial/_table_only.html', {
                'products': page_obj,
            }).content
        return HttpResponse(html, content_type='text/html; charset=utf-8')

    return render(request, 'sales/commercial/commercial_dashboard.html', {
        'products': page_obj,
        'warehouse': warehouse,
        'total_products': products.count(),
        'q': q,
        'type': ptype,
    })


@login_required
def commercial_products_table_partial(request):
    """Return only the products table partial for live search/filter updates."""
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return HttpResponse('', status=403)

    products = Product.objects.filter(is_active=True).select_related('brand').order_by('name')
    q = request.GET.get('q', '').strip()
    ptype = request.GET.get('type', '').strip()

    if q:
        if q.lower() == 'moto':
            products = products.filter(product_type='moto')
        elif q.lower() in ('pièce', 'piece', 'piÃ¨ce'):
            products = products.filter(product_type='piece')
        else:
            products = products.filter(Q(name__icontains=q) | Q(brand__name__icontains=q))

    if ptype:
        products = products.filter(product_type=ptype)

    paginator = Paginator(products, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    html = render(request, 'sales/commercial/_table_only.html', {
        'products': page_obj,
    }).content
    return HttpResponse(html, content_type='text/html; charset=utf-8')


# Removed standalone restock views; handled via dashboard modals


# commercial API views moved to apps/sales/views/commercial.py


@login_required
def commercial_journal(request):
    """Journal des approvisionnements réalisés par le Directeur Commercial.
    Affiche les demandes envoyées à l'entrepôt avec filtres et pagination.
    """
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

    # Pagination
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


@login_required
def commercial_stock(request):
    """Stock overview for Commercial Director: warehouse + all salespoints with filters and export."""
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return redirect('sales:dashboard')

    from apps.inventory.models import SalesPoint, SalesPointStock

    sp_id = int(request.GET.get('sp') or 0)
    view = (request.GET.get('view') or 'all').strip()  # all|warehouse|salespoint
    product_type = (request.GET.get('type') or 'all').strip()
    stock_filter = (request.GET.get('stock') or 'all').strip()  # all|low|zero|high|ok
    q = (request.GET.get('q') or '').strip()
    export = (request.GET.get('export') or '').strip() == 'csv'
    sort = (request.GET.get('sort') or '').strip()  # remaining|available|in|out|sp|product
    order = (request.GET.get('order') or 'desc').strip()  # asc|desc

    sps = SalesPoint.objects.order_by('is_warehouse', 'name')
    rows_qs = SalesPointStock.objects.select_related('salespoint','product','product__brand')
    if view == 'warehouse':
        rows_qs = rows_qs.filter(salespoint__is_warehouse=True)
    elif view == 'salespoint':
        rows_qs = rows_qs.filter(salespoint__is_warehouse=False)
    if sp_id:
        rows_qs = rows_qs.filter(salespoint_id=sp_id)
    if product_type in {'piece','moto'}:
        rows_qs = rows_qs.filter(product__product_type=product_type)
    if q:
        rows_qs = rows_qs.filter(Q(product__name__icontains=q) | Q(product__brand__name__icontains=q))

    rows = []
    counts = {'total': 0, 'zero': 0, 'low': 0, 'high': 0, 'ok': 0}
    for sps_row in rows_qs.order_by('salespoint__is_warehouse','salespoint__name','product__name')[:5000]:
        remaining = int(getattr(sps_row, 'remaining_qty', 0) or 0)
        available = int(getattr(sps_row, 'available_qty', 0) or 0)
        alert = int(getattr(sps_row, 'alert_qty', 5) or 5)
        status = 'high' if remaining > alert*3 else ('low' if 0 < remaining <= alert else ('zero' if remaining == 0 else 'ok'))
        if stock_filter != 'all' and status != stock_filter:
            continue
        row = {
            'salespoint': sps_row.salespoint,
            'product': sps_row.product,
            'brand': getattr(getattr(sps_row.product,'brand',None),'name',''),
            'remaining': remaining,
            'available': available,
            'opening': int(getattr(sps_row,'opening_qty',0) or 0),
            'in_qty': int(getattr(sps_row,'transfer_in',0) or 0),
            'out_qty': int(getattr(sps_row,'transfer_out',0) or 0),
            'status': status,
        }
        rows.append(row)
        counts['total'] += 1
        counts[status] = counts.get(status, 0) + 1

    # Sorting
    key_map = {
        'remaining': 'remaining',
        'available': 'available',
        'in': 'in_qty',
        'out': 'out_qty',
        'sp': lambda r: r['salespoint'].name.lower(),
        'product': lambda r: r['product'].name.lower(),
    }
    if sort in key_map:
        k = key_map[sort]
        rows.sort(key=(k if isinstance(k, str) else k), reverse=(order != 'asc'))

    if export:
        import csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename=stock_commercial.csv'
        w = csv.writer(resp)
        w.writerow(['Point de vente','Produit','Marque','Restant','Disponible','Ouverture','Entrées','Sorties','Statut'])
        for r in rows:
            w.writerow([r['salespoint'].name, r['product'].name, r['brand'], r['remaining'], r['available'], r['opening'], r['in_qty'], r['out_qty'], r['status']])
        return resp

    # Pagination simple
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    try:
        per = int(request.GET.get('per') or 50)
    except Exception:
        per = 50
    paginator = Paginator(rows, per)
    page_num = request.GET.get('page') or 1
    try:
        page = paginator.get_page(page_num)
    except (PageNotAnInteger, EmptyPage):
        page = paginator.get_page(1)

    return render(request, 'sales/commercial/commercial_stock.html', {
        'rows': page.object_list,
        'page_obj': page,
        'paginator': paginator,
        'sp_id': sp_id,
        'salespoints': sps,
        'q': q,
        'view': view,
        'type': product_type,
        'stock_filter': stock_filter,
        'per': per,
        'counts': counts,
        'sort': sort,
        'order': order,
    })

@login_required
def commercial_restock_stats(request):
    """Commercial Director: Statistics of restocking by product for warehouse and salespoints.
    Shows each restock event (date, product, quantity, destination), with filters and CSV export.
    """
    if not (request.user.is_superuser or getattr(request.user, 'role', '') == 'commercial_dir'):
        return redirect('sales:dashboard')

    from django.db.models import Q, F
    from apps.inventory.models import RestockRequest, RestockLine, SalesPoint

    scope = (request.GET.get('scope') or 'salespoint').strip()  # 'salespoint' | 'warehouse'
    sp_id = int(request.GET.get('sp') or 0)
    q = (request.GET.get('q') or '').strip()
    ptype = (request.GET.get('type') or 'all').strip()  # 'all' | 'piece' | 'moto'
    df = (request.GET.get('from') or '').strip()
    dt = (request.GET.get('to') or '').strip()
    export = (request.GET.get('export') or '') == 'csv'

    # Base queryset: join RestockLine -> RestockRequest
    rl = RestockLine.objects.select_related(
        'product', 'product__brand', 'request', 'request__salespoint'
    ).all()

    # Filter by scope: warehouse inbounds created by CD typically use reference starting with 'CD-'
    if scope == 'warehouse':
        rl = rl.filter(request__reference__startswith='CD-')
    else:
        # Salespoints restocked by warehouse: exclude warehouse-internal and CD inbound
        rl = rl.exclude(request__reference__startswith='WH-RQ-').exclude(request__reference__startswith='CD-')

    if sp_id:
        rl = rl.filter(request__salespoint_id=sp_id)

    if ptype in ('piece', 'moto'):
        rl = rl.filter(product__product_type=ptype)

    if q:
        rl = rl.filter(Q(product__name__icontains=q) | Q(product__brand__name__icontains=q))

    # Date range filters (by request.created_at)
    from django.utils.dateparse import parse_date
    start_date = parse_date(df) if df else None
    end_date = parse_date(dt) if dt else None
    if start_date:
        rl = rl.filter(request__created_at__date__gte=start_date)
    if end_date:
        rl = rl.filter(request__created_at__date__lte=end_date)

    # Build rows list for template/export
    rows = []
    for line in rl.order_by('-request__created_at')[:5000]:
        rows.append({
            'date': line.request.created_at,
            'salespoint': line.request.salespoint,
            'product': line.product,
            'brand': getattr(line.product.brand, 'name', ''),
            'qty': line.effective_quantity,
            'status': line.request.status,
        })

    # CSV export (all filtered rows)
    if export:
        import csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename=stats_ravitaillement.csv'
        w = csv.writer(resp)
        w.writerow(['Date','Point de vente','Produit','Marque','Quantité','Statut'])
        for r in rows:
            w.writerow([
                r['date'].strftime('%Y-%m-%d %H:%M'),
                r['salespoint'].name if r['salespoint'] else '—',
                r['product'].name,
                r['brand'],
                r['qty'],
                r['status'],
            ])
        return resp

    # Pagination
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    try:
        per = int(request.GET.get('per') or 50)
    except Exception:
        per = 50
    paginator = Paginator(rows, per)
    page_num = request.GET.get('page') or 1
    try:
        page = paginator.get_page(page_num)
    except (PageNotAnInteger, EmptyPage):
        page = paginator.get_page(1)

    return render(request, 'sales/commercial/commercial_restock_stats.html', {
        'rows': page.object_list,
        'page_obj': page,
        'paginator': paginator,
        'salespoints': SalesPoint.objects.order_by('is_warehouse', 'name'),
        'sp_id': sp_id,
        'q': q,
        'type': ptype,
        'scope': scope,
        'date_from': df,
        'date_to': dt,
    })