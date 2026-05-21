"""Dashboard routes with selectable date range."""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Shipment
from app.db.session import get_db
from app.agent.tools import (
    pkt_range,
    get_orders_in_range,
    get_bookings_in_range,
    get_critical_shipments,
    get_payment_overdue,
    get_inventory_ledger,
    get_returns_breakdown,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _empty_report() -> dict:
    return {
        "new_orders": {"total_orders": 0, "by_city": {}},
        "bookings_today": {"total_booked": 0, "by_courier": {}},
        "delivered_today": 0,
        "critical_undelivered": {"count": 0, "items": []},
        "payment_overdue": {"count": 0, "total_pending_pkr": 0, "items": []},
        "inventory_ledger": {
            "pcs_sent_to_courier": 0,
            "pcs_paid": 0,
            "pcs_return_in_process": 0,
            "pcs_return_to_shipper": 0,
            "pcs_received_back": 0,
            "pcs_pending": 0,
        },
        "returns": {"in_process": 0, "to_shipper": 0, "received_back": 0},
        "filter": {"period": "today", "label": "Today", "from": "", "to": ""},
    }


def _period_label(period: str, from_date: str, to_date: str, start, end) -> str:
    if period == "today":
        return "Today"
    if period == "last_7":
        return "Last 7 days"
    if period == "last_30":
        return "Last 30 days"
    if period == "custom":
        return f"{from_date} → {to_date}"
    return "Today"


def _build_report(db: Session, period: str = "today",
                  from_date: str = None, to_date: str = None) -> dict:
    """Aggregate everything the dashboard template needs into one dict."""
    report = _empty_report()
    try:
        start, end = pkt_range(period, from_date, to_date)
        label = _period_label(period, from_date, to_date, start, end)

        report["filter"] = {
            "period": period,
            "label": label,
            "from": from_date or "",
            "to": to_date or "",
        }
        report["new_orders"] = get_orders_in_range(db, start, end, label) or report["new_orders"]
        report["bookings_today"] = get_bookings_in_range(db, start, end, label) or report["bookings_today"]
        # These are all-time / status-based; not range-filtered:
        report["critical_undelivered"] = get_critical_shipments(db, min_days=3) or report["critical_undelivered"]
        report["payment_overdue"] = get_payment_overdue(db, min_days=7) or report["payment_overdue"]
        report["inventory_ledger"] = get_inventory_ledger(db) or report["inventory_ledger"]
        report["returns"] = get_returns_breakdown(db) or report["returns"]

        # Delivered count within the selected window
        report["delivered_today"] = (
            db.query(func.count(Shipment.id))
            .filter(
                Shipment.current_status == "delivered",
                Shipment.delivered_at >= start,
                Shipment.delivered_at < end,
            )
            .scalar()
            or 0
        )
    except Exception as e:
        report["_error"] = str(e)

    return report


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    period: str = "today",
    from_date: str = None,
    to_date: str = None,
    db: Session = Depends(get_db),
):
    # If custom dates provided, force period=custom
    if from_date and to_date:
        period = "custom"
    report = _build_report(db, period=period, from_date=from_date, to_date=to_date)
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "report": report}
    )


@router.get("/api/dashboard/summary")
async def summary(
    period: str = "today",
    from_date: str = None,
    to_date: str = None,
    db: Session = Depends(get_db),
):
    if from_date and to_date:
        period = "custom"
    return _build_report(db, period=period, from_date=from_date, to_date=to_date)
