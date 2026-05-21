"""Dashboard routes."""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Shipment
from app.db.session import get_db
from app.agent.tools import (
    get_daily_order_summary,
    get_courier_booking_summary,
    get_critical_shipments,
    get_payment_overdue,
    get_inventory_ledger,
    get_returns_breakdown,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _empty_report() -> dict:
    return {
        "new_orders": {"total": 0, "by_city": {}},
        "bookings_today": {"total": 0, "by_courier": {}},
        "delivered_today": 0,
        "critical_undelivered": {"count": 0, "items": []},
        "payment_overdue": {"count": 0, "total_amount": 0, "items": []},
        "inventory_ledger": {
            "pcs_shipped": 0,
            "pcs_paid": 0,
            "pcs_returned": 0,
            "pcs_return_in_process": 0,
            "pcs_return_to_shipper": 0,
            "pcs_pending": 0,
        },
        "returns": {"in_process": 0, "to_shipper": 0, "received_back": 0},
    }


def _build_report(db: Session) -> dict:
    """Aggregate everything the dashboard template needs into one dict."""
    report = _empty_report()
    try:
        report["new_orders"] = get_daily_order_summary(db, days_back=0) or report["new_orders"]
        report["bookings_today"] = get_courier_booking_summary(db, days_back=0) or report["bookings_today"]
        report["critical_undelivered"] = get_critical_shipments(db, min_days=3) or report["critical_undelivered"]
        report["payment_overdue"] = get_payment_overdue(db, min_days=7) or report["payment_overdue"]
        report["inventory_ledger"] = get_inventory_ledger(db) or report["inventory_ledger"]
        report["returns"] = get_returns_breakdown(db) or report["returns"]

        # "Today" in Pakistan time (UTC+5), then convert window back to UTC for DB.
        PKT_OFFSET = timedelta(hours=5)
        now_pkt = datetime.utcnow() + PKT_OFFSET
        today_pkt = now_pkt.replace(hour=0, minute=0, second=0, microsecond=0)
        start_today = today_pkt - PKT_OFFSET  # UTC timestamp of PKT midnight
        end_today = start_today + timedelta(days=1)
        report["delivered_today"] = (
            db.query(func.count(Shipment.id))
            .filter(
                Shipment.current_status == "delivered",
                Shipment.delivered_at >= start_today,
                Shipment.delivered_at < end_today,
            )
            .scalar()
            or 0
        )
    except Exception as e:
        # Empty DB on first run -> just return zeros so the page still loads.
        report["_error"] = str(e)

    return report


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    report = _build_report(db)
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "report": report}
    )


@router.get("/api/dashboard/summary")
async def summary(db: Session = Depends(get_db)):
    return _build_report(db)
