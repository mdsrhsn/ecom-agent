"""
Daily report generator.

Computes everything Mudassar asked for:
  - Today's new orders, broken down by city
  - Today's bookings, broken down by courier
  - Warehouse arrivals today
  - Critical undelivered (3+ days, not yet delivered/returned)
  - Payment overdue (7+ days post-delivery, no payment)
  - Inventory ledger: pieces shipped vs paid vs returned vs pending
  - Returns broken down: in-process vs to-shipper

The structured dict goes to the dashboard. Claude turns it into a
natural-language narrative for WhatsApp + email.
"""
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Order, Shipment, StatusEvent, Payment, DailyReport, ShipmentStatus
)
from app.config import settings


def _today_window():
    """Pakistan day window (UTC+5) — rough; refine if needed."""
    now = datetime.utcnow()
    start = (now - timedelta(hours=5)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert back to UTC
    return start - timedelta(hours=-5), start + timedelta(days=1) - timedelta(hours=-5)


def build_report(db: Session) -> dict:
    today_start, today_end = _today_window()

    # 1. New orders today — city breakdown
    new_orders = db.query(Order).filter(
        Order.created_at >= today_start,
        Order.created_at < today_end,
    ).all()
    city_breakdown: dict[str, int] = defaultdict(int)
    for o in new_orders:
        city_breakdown[o.city or "Unknown"] += 1

    # 2. Bookings today — courier breakdown
    booked_today = db.query(Shipment).filter(
        Shipment.booked_at >= today_start,
        Shipment.booked_at < today_end,
    ).all()
    courier_breakdown: dict[str, int] = defaultdict(int)
    for s in booked_today:
        courier_breakdown[s.courier] += 1

    # 3. Warehouse arrivals today
    arrived_today = db.query(Shipment).filter(
        Shipment.arrived_warehouse_at >= today_start,
        Shipment.arrived_warehouse_at < today_end,
    ).count()

    # 4. Delivered today
    delivered_today = db.query(Shipment).filter(
        Shipment.delivered_at >= today_start,
        Shipment.delivered_at < today_end,
    ).count()

    # 5. Critical undelivered (active list)
    critical_cutoff = datetime.utcnow() - timedelta(days=settings.critical_days)
    criticals = db.query(Shipment).filter(
        Shipment.is_critical == True,
        Shipment.delivered_at.is_(None),
        Shipment.returned_at.is_(None),
    ).all()
    critical_list = [
        {
            "cn": s.tracking_number,
            "courier": s.courier,
            "order": s.order.order_number if s.order else "",
            "customer": s.order.customer_name if s.order else "",
            "phone": s.order.customer_phone if s.order else "",
            "city": s.order.city if s.order else "",
            "cod": s.order.cod_amount if s.order else 0,
            "days_since_booking": (datetime.utcnow() - s.booked_at).days if s.booked_at else 0,
            "acknowledged": s.critical_acknowledged,
        }
        for s in criticals
    ]

    # 6. Payment overdue
    payment_cutoff = datetime.utcnow() - timedelta(days=settings.payment_overdue_days)
    overdue_q = db.query(Shipment).filter(
        Shipment.status == ShipmentStatus.DELIVERED.value,
        Shipment.delivered_at < payment_cutoff,
    ).all()
    overdue = [s for s in overdue_q if not s.payments]
    overdue_list = [
        {
            "cn": s.tracking_number,
            "courier": s.courier,
            "amount": s.order.cod_amount if s.order else 0,
            "days_since_delivery": (datetime.utcnow() - s.delivered_at).days,
            "order": s.order.order_number if s.order else "",
        }
        for s in overdue
    ]

    # 7. Inventory ledger — pieces accounting
    # Pcs shipped = sum of pieces for all shipments ever booked
    pcs_shipped = db.query(func.coalesce(func.sum(Shipment.pieces), 0)).filter(
        Shipment.booked_at.isnot(None),
    ).scalar() or 0

    # Pcs paid = pieces for shipments where payment exists
    paid_shipments = db.query(Shipment).join(Payment).distinct().all()
    pcs_paid = sum(s.pieces for s in paid_shipments)

    # Pcs returned (RETURNED — back in your hand)
    pcs_returned = db.query(func.coalesce(func.sum(Shipment.pieces), 0)).filter(
        Shipment.status == ShipmentStatus.RETURNED.value,
    ).scalar() or 0

    # Pcs in return-in-process (decision pending)
    pcs_return_in_process = db.query(func.coalesce(func.sum(Shipment.pieces), 0)).filter(
        Shipment.status == ShipmentStatus.RETURN_IN_PROCESS.value,
    ).scalar() or 0

    # Pcs return-to-shipper (confirmed coming back)
    pcs_return_to_shipper = db.query(func.coalesce(func.sum(Shipment.pieces), 0)).filter(
        Shipment.status == ShipmentStatus.RETURN_TO_SHIPPER.value,
    ).scalar() or 0

    pcs_pending = pcs_shipped - pcs_paid - pcs_returned

    return {
        "report_date": datetime.utcnow().isoformat(),
        "new_orders": {
            "total": len(new_orders),
            "by_city": dict(city_breakdown),
        },
        "bookings_today": {
            "total": len(booked_today),
            "by_courier": dict(courier_breakdown),
        },
        "arrived_warehouse_today": arrived_today,
        "delivered_today": delivered_today,
        "critical_undelivered": {
            "count": len(critical_list),
            "items": critical_list,
        },
        "payment_overdue": {
            "count": len(overdue_list),
            "total_amount": sum(x["amount"] for x in overdue_list),
            "items": overdue_list,
        },
        "inventory_ledger": {
            "pcs_shipped":            pcs_shipped,
            "pcs_paid":               pcs_paid,
            "pcs_returned":           pcs_returned,
            "pcs_return_in_process":  pcs_return_in_process,
            "pcs_return_to_shipper":  pcs_return_to_shipper,
            "pcs_pending":            pcs_pending,
        },
    }


def render_report_text(report: dict) -> str:
    """Plain-text version for WhatsApp/email. Keep it readable."""
    r = report
    lines = []
    lines.append(f"📦 DAILY ORDER REPORT — {datetime.utcnow().strftime('%d %b %Y')}")
    lines.append("")
    lines.append(f"🆕 New Orders Today: {r['new_orders']['total']}")
    for city, n in sorted(r['new_orders']['by_city'].items(), key=lambda x: -x[1]):
        lines.append(f"   • {city}: {n}")
    lines.append("")
    lines.append(f"🚚 Booked Today: {r['bookings_today']['total']}")
    for c, n in r['bookings_today']['by_courier'].items():
        lines.append(f"   • {c.upper()}: {n}")
    lines.append("")
    lines.append(f"🏬 Warehouse Arrivals: {r['arrived_warehouse_today']}")
    lines.append(f"✅ Delivered Today: {r['delivered_today']}")
    lines.append("")

    crit = r['critical_undelivered']
    if crit['count']:
        lines.append(f"🚨 CRITICAL (3+ days undelivered): {crit['count']}")
        for it in crit['items'][:10]:
            lines.append(
                f"   • {it['cn']} ({it['courier'].upper()}) "
                f"— {it['customer']} {it['phone']} — {it['days_since_booking']}d"
            )
        if crit['count'] > 10:
            lines.append(f"   …and {crit['count']-10} more")
        lines.append("")

    od = r['payment_overdue']
    if od['count']:
        lines.append(f"💰 PAYMENT OVERDUE (7+ days): {od['count']} (PKR {od['total_amount']:.0f})")
        for it in od['items'][:10]:
            lines.append(
                f"   • {it['cn']} ({it['courier'].upper()}) "
                f"— PKR {it['amount']:.0f} — {it['days_since_delivery']}d"
            )
        lines.append("")

    inv = r['inventory_ledger']
    lines.append("📊 INVENTORY LEDGER")
    lines.append(f"   • Shipped:           {inv['pcs_shipped']}")
    lines.append(f"   • Paid:              {inv['pcs_paid']}")
    lines.append(f"   • Returned (in hand):{inv['pcs_returned']}")
    lines.append(f"   • Return in process: {inv['pcs_return_in_process']}")
    lines.append(f"   • Return to shipper: {inv['pcs_return_to_shipper']}")
    lines.append(f"   • Pending:           {inv['pcs_pending']}")
    return "\n".join(lines)


async def generate_and_send_daily_report():
    """Called by scheduler."""
    from app.services.notifier import send_whatsapp, send_email
    db: Session = SessionLocal()
    try:
        report = build_report(db)
        text = render_report_text(report)

        wa_ok = await send_whatsapp(text)
        em_ok = send_email(
            f"Daily Order Report — {datetime.utcnow().strftime('%d %b %Y')}",
            text,
        )

        db.add(DailyReport(
            report_date=datetime.utcnow(),
            summary_json=report,
            narrative=text,
            sent_to_whatsapp=wa_ok,
            sent_to_email=em_ok,
        ))
        db.commit()
        return text
    finally:
        db.close()
