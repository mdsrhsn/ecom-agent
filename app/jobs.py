"""Scheduled background jobs."""
from datetime import datetime, timedelta
from app.db.session import SessionLocal
from app.db.models import Shipment, AlertLog
from app.agent.tools import (
    refresh_shipment_tracking,
    get_daily_order_summary,
    get_courier_booking_summary,
    get_critical_shipments,
    get_payment_overdue,
    get_inventory_ledger,
    get_returns_breakdown,
)
from app.services import whatsapp, email as email_svc
from app.config import settings


TERMINAL_STATUSES = ("delivered", "received_back", "cancelled", "lost")


async def poll_active_shipments():
    db = SessionLocal()
    try:
        active = (
            db.query(Shipment)
            .filter(~Shipment.current_status.in_(TERMINAL_STATUSES))
            .all()
        )
        print(f"[poll] refreshing {len(active)} active shipments")
        for sh in active:
            try:
                await refresh_shipment_tracking(db, sh.tracking_number)
            except Exception as e:
                print(f"[poll] {sh.tracking_number} failed: {e}")

        _flag_critical(db)
        _flag_payment_overdue(db)
        db.commit()
    finally:
        db.close()


def _flag_critical(db):
    cutoff = datetime.utcnow() - timedelta(days=3)
    shipments = db.query(Shipment).filter(
        Shipment.booked_at <= cutoff,
        Shipment.current_status.in_(["booked", "arrived_warehouse", "in_transit", "return_in_process"]),
        Shipment.delivered_at.is_(None),
    ).all()
    for sh in shipments:
        sh.is_critical = True


def _flag_payment_overdue(db):
    from app.db.models import Payment
    cutoff = datetime.utcnow() - timedelta(days=7)
    shipments = (
        db.query(Shipment)
        .outerjoin(Payment, Payment.shipment_id == Shipment.id)
        .filter(
            Shipment.current_status == "delivered",
            Shipment.delivered_at <= cutoff,
            Payment.id.is_(None),
        )
        .all()
    )
    for sh in shipments:
        sh.is_payment_overdue = True


async def daily_summary():
    db = SessionLocal()
    try:
        orders = get_daily_order_summary(db, days_back=0)
        bookings = get_courier_booking_summary(db, days_back=0)
        critical = get_critical_shipments(db, min_days=3)
        overdue = get_payment_overdue(db, min_days=7)
        ledger = get_inventory_ledger(db)
        returns = get_returns_breakdown(db)

        text = _format_summary_text(orders, bookings, critical, overdue, ledger, returns)
        html = "<pre style='font-family:monospace;font-size:13px'>" + text + "</pre>"

        await whatsapp.broadcast(text)

        recipients = [settings.OWNER_EMAIL] if settings.OWNER_EMAIL else []
        if settings.TEAM_EMAILS:
            recipients += [e.strip() for e in settings.TEAM_EMAILS.split(",") if e.strip()]
        if recipients:
            email_svc.send_email(
                recipients,
                subject=f"Daily Ecom Report — {datetime.now().strftime('%d %b %Y')}",
                body=text,
                html=html,
            )

        db.add(AlertLog(
            alert_type="daily_summary",
            channel="whatsapp+email",
            recipient=",".join(settings.all_notify_phones),
            message_preview=text[:500],
        ))
        db.commit()
    finally:
        db.close()


def _format_summary_text(orders, bookings, critical, overdue, ledger, returns) -> str:
    lines = [
        f"*Daily Report — {orders['date']}*",
        "",
        f"*New orders today:* {orders['total_orders']}",
    ]
    for city, cnt in list(orders["by_city"].items())[:8]:
        lines.append(f"   - {city}: {cnt}")
    lines.append("")
    lines.append(f"*Booked to couriers today:* {bookings['total_booked']}")
    for courier, info in bookings["by_courier"].items():
        lines.append(f"   - {courier}: {info['shipments']} shipments / {info['pcs']} pcs")
    lines.append("")
    lines.append(f"*CRITICAL (3+ days):* {critical['count']}")
    for item in critical["items"][:10]:
        lines.append(
            f"   - {item['tracking']} | {item['customer']} ({item['phone']}) | "
            f"{item['city']} | {item['days_since_booking']}d | {item['status']}"
        )
    lines.append("")
    lines.append(
        f"*Payment overdue (7+ days):* {overdue['count']} parcels, "
        f"PKR {overdue['total_pending_pkr']:,.0f}"
    )
    for item in overdue["items"][:10]:
        lines.append(
            f"   - {item['tracking']} | {item['courier']} | "
            f"{item['days_since_delivery']}d | PKR {item['amount_due']:,.0f}"
        )
    lines.append("")
    lines.append("*Inventory Ledger:*")
    lines.append(f"   - Sent to courier: {ledger['pcs_sent_to_courier']} pcs")
    lines.append(f"   - Paid: {ledger['pcs_paid']} pcs")
    lines.append(f"   - Return in process: {ledger['pcs_return_in_process']} pcs")
    lines.append(f"   - Return to shipper: {ledger['pcs_return_to_shipper']} pcs")
    lines.append(f"   - Received back: {ledger['pcs_received_back']} pcs")
    lines.append(f"   - *Pending: {ledger['pcs_pending']} pcs*")
    lines.append("")
    lines.append("*Returns breakdown:*")
    lines.append(f"   - In process: {returns['return_in_process']['shipments']} ({returns['return_in_process']['pcs']} pcs)")
    lines.append(f"   - To shipper: {returns['return_to_shipper']['shipments']} ({returns['return_to_shipper']['pcs']} pcs)")
    lines.append(f"   - Received back: {returns['received_back']['shipments']} ({returns['received_back']['pcs']} pcs)")
    return "\n".join(lines)
