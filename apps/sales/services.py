# apps/sales/services.py
import re
from decimal import Decimal
from typing import Iterable, Tuple, Optional
from collections import defaultdict

from django.db import transaction
from django.db import IntegrityError
from django.db.models import Max
from django.utils import timezone

from apps.sales.models import Sale, SaleItem, CancellationRequest, CancellationLine
from apps.inventory.models import SalesPointStock


class SaleError(Exception):
    """Raised for functional sale errors (stock, payload, etc.)."""
    pass


from django.core.exceptions import ObjectDoesNotExist


def _lock_sps_or_error(*, salespoint, product_id: int) -> SalesPointStock:
    """Fetch and lock SalesPointStock for update with a friendly error."""
    try:
        return SalesPointStock.objects.select_for_update().get(
            salespoint=salespoint, product_id=product_id
        )
    except ObjectDoesNotExist:
        raise SaleError(f"Produit #{product_id} indisponible à ce point de vente.")


def generate_invoice_number(salespoint, kind: str) -> str:
    """
    Format: PP-DDMMYY-K-0001
    - PP: first two letters from the first meaningful word of the salespoint *name*,
          ignoring common prefixes like 'SP', 'PDV', 'POS', 'PV'. Fallback: 'EC'.
    - DDMMYY: local date (e.g., 160825 for 16-08-2025)
    - K: kind code ('P' for pièces, 'M' for motos)
    - ####: zero-padded daily sequence per salespoint + kind + date
    """
    # Kind as a single uppercased letter
    k = (kind or "P").upper()[:1]

    # Build prefix from salespoint *name*
    sp_name = (getattr(salespoint, "name", "") or "").upper()
    # Split into alphabetic tokens (handles spaces, dashes, etc.)
    tokens = [t for t in re.split(r"[^A-Z]+", sp_name) if t]
    stop = {"SP", "PDV", "POS", "PV", "AGENCE", "DEPOT"}
    meaningful = next((t for t in tokens if t not in stop and len(t) >= 2), "")

    if meaningful:
        pp = meaningful[:2]
    else:
        # Fallback: take first two letters from letters-only string
        letters_only = "".join(ch for ch in sp_name if ch.isalpha())
        pp = (letters_only[:2] or "EC")

    # Date part (DDMMYY)
    today = timezone.localdate()
    date_part = today.strftime("%d%m%y")

    base = f"{pp}-{date_part}-{k}-"

    # Find max existing number for this base and increment
    mx = (
        Sale.objects
        .filter(salespoint=salespoint, number__startswith=base)
        .aggregate(mx=Max("number"))
        .get("mx")
    )
    if mx:
        try:
            last_seq = int(mx.split("-")[-1])
        except Exception:
            last_seq = 0
    else:
        last_seq = 0

    seq = last_seq + 1
    return f"{base}{seq:04d}"


def _normalize_items(items: Iterable[dict]) -> Tuple[Tuple[int, int, Decimal], ...]:
    bucket = defaultdict(lambda: {"qty": 0, "up": None})
    for it in items:
        pid = int(it.get("product_id") or 0)
        qty = int(it.get("qty") or 0)
        up = Decimal(str(it.get("unit_price") or 0))
        if pid <= 0 or qty <= 0:
            raise SaleError("Article invalide (produit/quantité).")
        if up <= 0:
            raise SaleError("Prix unitaire invalide.")
        if bucket[pid]["up"] not in (None, up):
            raise SaleError(f"Prix incohérent pour le produit #{pid}.")
        bucket[pid]["qty"] += qty
        bucket[pid]["up"] = up
    return tuple((pid, v["qty"], v["up"]) for pid, v in bucket.items())


@transaction.atomic
def create_sale_draft(*, salespoint, seller, kind: str, customer_name: str,
                      customer_phone: str, payment_type: str, items: list) -> Sale:
    """
    Create a sale in status "awaiting_cashier" and RESERVE stock at the sales point.
    Each item: {product_id, qty, unit_price}
    Reservation strategy (preferred):
      - use SalesPointStock.reserved_qty if available
      - available = remaining_qty - reserved_qty
      - on draft: reserved_qty += qty
      Backward-compatible fallback (older schema without reserved_qty):
      - decrement remaining_qty immediately (soft lock) and revert on cancel.
    """
    if not items:
        raise SaleError("Aucun article fourni.")

    norm_items = _normalize_items(items)

    # Enforce one-moto-per-sale (motorcycle sales carry unique chassis/engine data)
    if (kind or "").upper() == "M":
        if len(norm_items) != 1:
            raise SaleError("Une vente de moto doit contenir une seule moto (Qté = 1).")
        _pid, _qty, _up = norm_items[0]
        if _qty != 1:
            raise SaleError("La quantité d'une moto doit être 1.")

    total = sum((up * qty) for (_, qty, up) in norm_items)
    total = total.quantize(Decimal("1"))

    # Generate invoice number with retry in case of race collisions
    sale = None
    for _ in range(4):
        number = generate_invoice_number(salespoint, kind)
        try:
            sale = Sale.objects.create(
                salespoint=salespoint,
                seller=seller,
                kind=kind.upper(),
                number=number,
                customer_name=customer_name or "DIVERS",
                customer_phone=customer_phone or "",
                payment_type=payment_type or "cash",
                status="awaiting_cashier",
                total_amount=total,
            )
            break
        except IntegrityError:
            # Collision on unique sale number: retry with a new number
            continue
    if sale is None:
        raise SaleError("Impossible de générer un numéro de facture unique. Veuillez réessayer.")

    # Reserve stock per line
    for pid, qty, up in norm_items:
        sps = _lock_sps_or_error(salespoint=salespoint, product_id=pid)
        # Try reservation field if present
        if hasattr(sps, "reserved_qty"):
            current_reserved = int(getattr(sps, "reserved_qty") or 0)
            available = int(sps.remaining_qty) - current_reserved
            if available < qty:
                raise SaleError(f"Stock insuffisant pour le produit #{pid}.")
            sps.reserved_qty = current_reserved + qty
            sps.save(update_fields=["reserved_qty"])
        else:
            # Fallback (legacy): validate availability only; do not write computed properties
            if int(sps.remaining_qty) < qty:
                raise SaleError(f"Stock insuffisant pour le produit #{pid}.")
            # No mutation here; stock will be decremented later at commit time.

        SaleItem.objects.create(
            sale=sale,
            product_id=pid,
            quantity=qty,
            unit_price=up,
            line_total=(up * qty).quantize(Decimal("1")),
        )

    return sale


@transaction.atomic
def approve_sale(*, sale: Sale, amount_received: Optional[Decimal] = None, cashier=None) -> dict:
    """Finalize a reserved sale: commit stock and mark as approved.

    Also stamps cashier/approved_at/received_amount when fields exist.
    Returns a dict with the calculated change: {"change": Decimal}.
    """
    total = (sale.total_amount or Decimal("0")).quantize(Decimal("1"))
    amt = (amount_received if amount_received is not None else Decimal("0")).quantize(Decimal("1"))

    # For immediate cash payments, ensure received amount is enough
    if getattr(sale, "payment_type", "cash") == "cash" and amt < total:
        raise SaleError("Montant reçu insuffisant.")

    if sale.status != "awaiting_cashier":
        # Nothing to do; still compute and return change for the caller
        return {"change": amt - total}

    lines = list(sale.items.all())
    for it in lines:
        sps = _lock_sps_or_error(salespoint=sale.salespoint, product_id=it.product_id)
        if hasattr(sps, "reserved_qty"):
            cur_res = int(getattr(sps, "reserved_qty") or 0)
            if cur_res < it.quantity:
                raise SaleError(f"Réservation insuffisante pour le produit #{it.product_id}.")
            # Only release the reservation; physical stock decrement occurs in commit_for_sale()
            sps.reserved_qty = cur_res - it.quantity
            sps.save(update_fields=["reserved_qty"])
        else:
            # Legacy fallback: stock commit handled downstream.
            pass

    # Stamp approval metadata if fields exist
    try:
        from django.utils import timezone as _tz
        if hasattr(sale, "cashier"):
            sale.cashier = cashier
        if hasattr(sale, "approved_at") and not getattr(sale, "approved_at", None):
            sale.approved_at = _tz.now()
        if hasattr(sale, "received_amount") and amount_received is not None:
            sale.received_amount = amt
    except Exception:
        # Non-fatal; proceed
        pass

    sale.status = "approved"
    update_fields = ["status"]
    if hasattr(sale, "cashier"):
        update_fields.append("cashier")
    if hasattr(sale, "approved_at"):
        update_fields.append("approved_at")
    if hasattr(sale, "received_amount") and amount_received is not None:
        update_fields.append("received_amount")
    sale.save(update_fields=update_fields)

    return {"change": amt - total}


@transaction.atomic
def cancel_sale(*, sale: Sale) -> Sale:
    """Cancel a draft/awaiting sale: release reservation (or restore legacy lock)."""
    if sale.status not in ("awaiting_cashier", "draft"):
        return sale

    lines = list(sale.items.all())
    for it in lines:
        sps = _lock_sps_or_error(salespoint=sale.salespoint, product_id=it.product_id)
        if hasattr(sps, "reserved_qty"):
            cur_res = int(getattr(sps, "reserved_qty") or 0)
            # Release reservation only
            new_res = max(0, cur_res - it.quantity)
            if new_res != cur_res:
                sps.reserved_qty = new_res
                sps.save(update_fields=["reserved_qty"])
        else:
            # Legacy fallback: nothing to undo; remaining_qty is computed and was not mutated at draft.
            pass

    sale.status = "cancelled"
    update_fields = ["status"]
    try:
        from django.utils import timezone as _tz
        if hasattr(sale, "cancelled_at") and not getattr(sale, "cancelled_at", None):
            sale.cancelled_at = _tz.now()
            update_fields.append("cancelled_at")
    except Exception:
        pass
    sale.save(update_fields=update_fields)
    return sale

# -----------------------------
# Cancellation helpers (UI flow)
# -----------------------------
from django.utils import timezone as _tz


def find_sale_by_number(*, salespoint, number: str) -> Sale:
    """Fetch a sale by its human invoice number for a given salespoint."""
    try:
        return (
            Sale.objects.select_for_update()
            .get(salespoint=salespoint, number=str(number).strip())
        )
    except Sale.DoesNotExist:
        raise SaleError("Reçu introuvable pour ce point de vente.")


def _recompute_sale_totals(sale: Sale) -> None:
    """Recompute total (and profit if model supports it) from current items."""
    items = list(sale.items.all())
    total = sum((it.line_total or Decimal("0")) for it in items)
    total = Decimal(total).quantize(Decimal("1"))

    update_fields = ["total_amount"]
    sale.total_amount = total

    # Optional: recompute profit if fields exist
    try:
        # If SaleItem has `unit_cost`, use it; else skip silently
        costs = []
        for it in items:
            if hasattr(it, "unit_cost") and it.unit_cost is not None:
                costs.append(Decimal(it.unit_cost) * int(it.quantity))
        if costs and hasattr(sale, "profit_amount"):
            cost_total = sum(costs)
            profit = Decimal(sale.total_amount) - Decimal(cost_total)
            sale.profit_amount = profit.quantize(Decimal("1"))
            if hasattr(sale, "profit_currency") and not sale.profit_currency:
                # Try to mirror sale currency if present
                if hasattr(sale, "currency"):
                    sale.profit_currency = sale.currency
            update_fields.append("profit_amount")
            if hasattr(sale, "profit_currency"):
                update_fields.append("profit_currency")
    except Exception:
        # Do not block cancellation flows on profit math
        pass

    sale.save(update_fields=update_fields)


@transaction.atomic
def cancel_sale_same_day(*, sale: Sale, item_quantities: dict | None, actor=None, reason: str = "") -> Sale:
    """
    Cancel items from an *approved* sale on the same day.
    - item_quantities: dict of {sale_item_id: qty_to_cancel}. If None, cancel all lines.
    Stock is re-credited to SalesPointStock.remaining_qty immediately.
    If all lines are removed, the sale is marked as 'cancelled'.
    """
    # Guard: only allow same-day quick cancel
    if hasattr(sale, "approved_at"):
        approved_date = sale.approved_at.date() if sale.approved_at else None
        if approved_date and approved_date != timezone.localdate():
            raise SaleError("Annulation instantanée limitée aux ventes du jour.")

    if sale.status != "approved":
        # We only support immediate cancel on already approved sales here.
        # Drafts should use the existing cancel_sale() flow.
        raise SaleError("Seules les ventes approuvées peuvent être annulées ici.")

    # Build selection of items
    sale_items = {it.id: it for it in sale.items.select_related(None)}
    selections: dict[int, int] = {}
    if item_quantities:
        for sid, qty in item_quantities.items():
            sid = int(sid)
            qty = int(qty)
            if sid not in sale_items:
                raise SaleError(f"Ligne de vente inconnue (id={sid}).")
            if qty <= 0 or qty > int(sale_items[sid].quantity):
                raise SaleError("Quantité d'annulation invalide.")
            selections[sid] = qty
    else:
        # Cancel full sale
        selections = {sid: int(it.quantity) for sid, it in sale_items.items()}

    # Re-credit stock and shrink/delete items
    for sid, qty in selections.items():
        it = sale_items[sid]
        sps = _lock_sps_or_error(salespoint=sale.salespoint, product_id=it.product_id)
        # Return stock by decreasing sold count; do not write to computed remaining_qty
        try:
            cur_sold = int(getattr(sps, "sold_qty") or 0)
            new_sold = max(0, cur_sold - int(qty))
            if new_sold != cur_sold:
                sps.sold_qty = new_sold
                sps.save(update_fields=["sold_qty"])
        except Exception:
            # If sold_qty field doesn't exist, silently skip; remaining is computed
            pass

        if qty == int(it.quantity):
            it.delete()
        else:
            it.quantity = int(it.quantity) - qty
            it.line_total = (Decimal(it.unit_price) * int(it.quantity)).quantize(Decimal("1"))
            # Optional: adjust cost-based fields if present
            if hasattr(it, "unit_cost") and it.unit_cost is not None:
                if hasattr(it, "line_cost"):
                    it.line_cost = Decimal(it.unit_cost) * int(it.quantity)
            it.save(update_fields=["quantity", "line_total"] + (["line_cost"] if hasattr(it, "line_cost") else []))

    # If no items remain, mark sale cancelled
    if not sale.items.exists():
        sale.status = "cancelled"
        sale.cancelled_at = _tz.now() if hasattr(sale, "cancelled_at") else None
        fields = ["status"]
        if hasattr(sale, "cancelled_at"):
            fields.append("cancelled_at")
        sale.save(update_fields=fields)
    else:
        # Use domain method when available to keep cost/profit in sync
        try:
            if hasattr(sale, "recalc_total") and callable(getattr(sale, "recalc_total")):
                sale.recalc_total()
                sale.save(update_fields=[f for f in ["total_amount", "total_cost", "gross_profit"] if hasattr(sale, f)])
            else:
                _recompute_sale_totals(sale)
        except Exception:
            _recompute_sale_totals(sale)

    return sale


@transaction.atomic
def create_cancellation_request(*, sale: Sale, item_quantities: dict | None, requested_by, reason: str = "") -> CancellationRequest:
    """
    Create a pending CancellationRequest for a sale not from today (or requiring approval).
    - item_quantities: dict of {sale_item_id: qty_to_cancel}. If None, request full cancel.
    """
    if sale.status != "approved":
        raise SaleError("Seules les ventes approuvées peuvent être demandées en annulation.")

    if not (reason or "").strip():
        raise SaleError("Un motif d'annulation est requis.")

    req = CancellationRequest.objects.create(
        sale=sale,
        requested_by=requested_by,
        status="pending",
        reason=reason.strip(),
    )

    sale_items = {it.id: it for it in sale.items.all()}
    if item_quantities:
        for sid, qty in item_quantities.items():
            sid = int(sid)
            qty = int(qty)
            if sid not in sale_items:
                raise SaleError(f"Ligne de vente inconnue (id={sid}).")
            if qty <= 0 or qty > int(sale_items[sid].quantity):
                raise SaleError("Quantité d'annulation invalide.")
            it = sale_items[sid]
            CancellationLine.objects.create(
                request=req,
                sale_item=it,
                quantity=qty,
                unit_price=Decimal(it.unit_price or 0),
                unit_cost=Decimal(getattr(it, "unit_cost", Decimal("0.00")) or 0),
                line_total=(Decimal(it.unit_price or 0) * qty).quantize(Decimal("1")),
            )
    else:
        # Request full cancel of every line
        for it in sale_items.values():
            q = int(it.quantity)
            CancellationLine.objects.create(
                request=req,
                sale_item=it,
                quantity=q,
                unit_price=Decimal(it.unit_price or 0),
                unit_cost=Decimal(getattr(it, "unit_cost", Decimal("0.00")) or 0),
                line_total=(Decimal(it.unit_price or 0) * q).quantize(Decimal("1")),
            )

    return req


@transaction.atomic
def approve_cancellation_request(*, request: CancellationRequest, approver) -> CancellationRequest:
    """Approve a pending request and apply stock & sale adjustments."""
    if request.status != "pending":
        return request

    # Build map {sale_item_id: qty}
    qmap: dict[int, int] = {}
    for ln in request.lines.all():
        sid = getattr(ln, "sale_item_id", None)
        if sid is None:
            # Try to match by product if sale_item FK not present
            it = request.sale.items.filter(product_id=getattr(ln, "product_id", None)).first()
            if not it:
                raise SaleError("Impossible de retrouver une ligne correspondante pour l'annulation.")
            sid = it.id
        sid = int(sid)
        qmap[sid] = int(qmap.get(sid, 0)) + int(ln.quantity)

    cancel_sale_same_day(sale=request.sale, item_quantities=qmap, actor=approver)

    request.status = "approved"
    request.approver = approver if hasattr(request, "approver") else None
    request.approved_at = _tz.now() if hasattr(request, "approved_at") else None
    fields = ["status"]
    if hasattr(request, "approver"):
        fields.append("approver")
    if hasattr(request, "approved_at"):
        fields.append("approved_at")
    request.save(update_fields=fields)

    return request