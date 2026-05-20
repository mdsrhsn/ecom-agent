"""
Notification service.

Sends WhatsApp Business Cloud messages + SMTP email.
Deduplicates via AlertLog so the same shipment doesn't spam.
"""
from datetime import datetime
import logging
import smtplib
from email.mime.text import MIMEText
import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AlertLog, Shipment

log = logging.getLogger(__name__)


# ---------- WhatsApp ----------

async def send_whatsapp(text: str, recipients: list[str] | None = None) -> bool:
    if not settings.whatsapp_token or not settings.whatsapp_phone_id:
        log.warning("WhatsApp not configured; skipping")
        return False

    to_list = recipients or [
        r.strip() for r in settings.whatsapp_recipients.split(",") if r.strip()
    ]
    if not to_list:
        return False

    url = f"https://graph.facebook.com/v22.0/{settings.whatsapp_phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }
    success = True
    async with httpx.AsyncClient(timeout=15.0) as client:
        for to in to_list:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            }
            try:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
            except Exception as e:
                log.error("WhatsApp send failed to %s: %s", to, e)
                success = False
    return success


# ---------- Email ----------

def send_email(subject: str, body: str, recipients: list[str] | None = None) -> bool:
    if not settings.smtp_host or not settings.smtp_user:
        log.warning("SMTP not configured; skipping")
        return False

    to_list = recipients or [
        r.strip() for r in settings.email_recipients.split(",") if r.strip()
    ]
    if not to_list:
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = ", ".join(to_list)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
            s.starttls()
            s.login(settings.smtp_user, settings.smtp_pass)
            s.sendmail(settings.smtp_user, to_list, msg.as_string())
        return True
    except Exception as e:
        log.error("Email failed: %s", e)
        return False


# ---------- Domain alerts ----------

async def send_critical_alert(db: Session, sh: Shipment):
    """A shipment crossed the critical threshold (3+ days undelivered)."""
    # Dedupe — don't send same critical alert twice
    existing = db.query(AlertLog).filter_by(
        shipment_id=sh.id, alert_type="critical_undelivered"
    ).first()
    if existing:
        return

    order = sh.order
    msg = (
        f"🚨 CRITICAL: parcel undelivered for {settings.critical_days}+ days\n"
        f"Order: {order.order_number} | {order.customer_name}\n"
        f"City: {order.city} | COD: PKR {order.cod_amount:.0f}\n"
        f"Courier: {sh.courier.upper()} | CN: {sh.tracking_number}\n"
        f"Booked: {sh.booked_at.strftime('%Y-%m-%d') if sh.booked_at else '—'}\n"
        f"Customer: {order.customer_phone}\n"
        f"👉 Team needs to call and follow up."
    )
    await send_whatsapp(msg)
    send_email(f"[CRITICAL] {order.order_number} undelivered", msg)
    db.add(AlertLog(
        shipment_id=sh.id, alert_type="critical_undelivered",
        channel="whatsapp+email", payload=msg,
    ))


async def send_payment_overdue_alert(db: Session, sh: Shipment):
    existing = db.query(AlertLog).filter_by(
        shipment_id=sh.id, alert_type="payment_overdue"
    ).first()
    if existing:
        return

    order = sh.order
    days = (datetime.utcnow() - sh.delivered_at).days if sh.delivered_at else 0
    msg = (
        f"💰 PAYMENT OVERDUE\n"
        f"CN: {sh.tracking_number} ({sh.courier.upper()})\n"
        f"Delivered {days} days ago | COD: PKR {order.cod_amount:.0f}\n"
        f"Order: {order.order_number}\n"
        f"👉 Follow up with courier finance team."
    )
    await send_whatsapp(msg)
    send_email(f"[Payment Overdue] {sh.tracking_number}", msg)
    db.add(AlertLog(
        shipment_id=sh.id, alert_type="payment_overdue",
        channel="whatsapp+email", payload=msg,
    ))
