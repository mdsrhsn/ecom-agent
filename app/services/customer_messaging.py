"""
Customer WhatsApp automation agent.

When courier status changes (via poller / refresh), send the matching
Roman Urdu WhatsApp update to the customer. On delivery, ask for feedback;
after they reply, send a follow-up offer for more help / products.

Dedupes via CustomerMessageLog so the same status message is never
sent twice for one shipment.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    AlertLog,
    CustomerConversation,
    CustomerMessageLog,
    Order,
    Shipment,
)
from app.services import whatsapp

log = logging.getLogger(__name__)

# Statuses that trigger an automatic customer WhatsApp update
CUSTOMER_NOTIFY_STATUSES = {
    "booked",
    "arrived_warehouse",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "return_in_process",
    "return_to_shipper",
    "received_back",
    "cancelled",
}


def normalize_phone(phone: str | None) -> str | None:
    """Normalize PK / international phones to WhatsApp digits (e.g. 923001234567)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = "92" + digits[1:]
    elif len(digits) == 10 and digits.startswith("3"):
        digits = "92" + digits
    if len(digits) < 10:
        return None
    return digits


def _first_name(order: Order | None) -> str:
    if not order or not order.customer_name:
        return "Customer"
    return order.customer_name.strip().split()[0]


def _brand() -> str:
    return settings.STORE_NAME or "Women Comforts"


def build_status_message(status: str, order: Order, shipment: Shipment) -> str | None:
    """Return Roman Urdu WhatsApp body for a normalized status, or None to skip."""
    name = _first_name(order)
    brand = _brand()
    order_no = order.order_number or ""
    tracking = shipment.tracking_number or ""
    courier = (shipment.courier or "courier").upper()
    city = order.city or ""

    templates = {
        "booked": (
            f"Assalam o Alaikum {name} ji 👋\n\n"
            f"Aapka order {order_no} *{brand}* se book ho gaya hai.\n"
            f"Courier: {courier}\n"
            f"Tracking: {tracking}\n\n"
            f"Hum aapko har update WhatsApp par bhejte rahenge. Shukriya!"
        ),
        "arrived_warehouse": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka parcel ({tracking}) courier warehouse mein receive ho gaya hai.\n"
            f"Jaldi hi aapki taraf dispatch ho jayega. 📦"
        ),
        "in_transit": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka order {order_no} ab *dispatch / in-transit* hai "
            f"({courier} — {tracking}).\n"
            f"{'City: ' + city + chr(10) if city else ''}"
            f"Jald aap tak pohonch jayega InshaAllah. 🚚"
        ),
        "out_for_delivery": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka parcel *aaj delivery ke liye nikal chuka hai* "
            f"({tracking}).\n"
            f"Please phone on rakhein — rider aap se contact karega. 📍"
        ),
        "delivered": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka order {order_no} *deliver* ho chuka hai. 🎉\n"
            f"Tracking: {tracking}\n\n"
            f"Baraye meherbani humein feedback dein:\n"
            f"1️⃣ Product kaisa laga?\n"
            f"2️⃣ Sab kuch theek mila ya koi issue tha?\n"
            f"3️⃣ Quality se mutmaeen hain?\n\n"
            f"Agar koi masla ho to yahan likh dein — hum madad karenge."
        ),
        "return_in_process": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapke parcel ({tracking}) ka *return process* shuru ho gaya hai.\n"
            f"Agar ye ghalti se hua hai ya aap delivery chahte hain, "
            f"to please humein yahan reply karein ya call karein. "
            f"Hum madad karenge."
        ),
        "return_to_shipper": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka parcel ({tracking}) *return to shipper* confirm ho gaya hai "
            f"aur wapas aa raha hai.\n"
            f"Agar dubara order chahiye ya koi sawal ho to humein bata dein."
        ),
        "received_back": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka returned parcel ({tracking}) humare paas wapas aa gaya hai.\n"
            f"Agar naya order ya refund / exchange chahiye to yahan likh dein."
        ),
        "cancelled": (
            f"Assalam o Alaikum {name} ji,\n\n"
            f"Aapka order {order_no} / parcel {tracking} *cancel* ho gaya hai.\n"
            f"Agar ye ghalti se hua hai to please humein reply karein."
        ),
    }
    return templates.get(status)


def build_followup_message(order: Order) -> str:
    name = _first_name(order)
    brand = _brand()
    return (
        f"Shukriya {name} ji aapke feedback ka! 🙏\n\n"
        f"Agar aapko *{brand}* se kisi aur product ki zarurat ho, "
        f"ya koi madad chahiye (size, exchange, order tracking), "
        f"to yahan bata dein — hum khushi se help karenge."
    )


def _already_sent(db: Session, shipment_id: int, message_type: str) -> bool:
    return (
        db.query(CustomerMessageLog)
        .filter(
            CustomerMessageLog.shipment_id == shipment_id,
            CustomerMessageLog.message_type == message_type,
            CustomerMessageLog.direction == "outbound",
            CustomerMessageLog.success.is_(True),
        )
        .first()
        is not None
    )


def _get_or_create_conversation(
    db: Session, shipment: Shipment, order: Order, phone: str
) -> CustomerConversation:
    conv = (
        db.query(CustomerConversation)
        .filter(CustomerConversation.shipment_id == shipment.id)
        .first()
    )
    if conv:
        if phone and conv.phone != phone:
            conv.phone = phone
        return conv
    conv = CustomerConversation(
        shipment_id=shipment.id,
        order_id=order.id if order else None,
        phone=phone,
        state="idle",
    )
    db.add(conv)
    db.flush()
    return conv


async def _send_and_log(
    db: Session,
    *,
    phone: str,
    body: str,
    message_type: str,
    shipment: Shipment | None = None,
    order: Order | None = None,
    status_key: str | None = None,
) -> dict:
    result = await whatsapp.send_message(phone, body)
    ok = "error" not in result
    wa_id = None
    if ok:
        try:
            wa_id = result["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            wa_id = None

    db.add(
        CustomerMessageLog(
            shipment_id=shipment.id if shipment else None,
            order_id=order.id if order else (shipment.order_id if shipment else None),
            phone=phone,
            direction="outbound",
            message_type=message_type,
            status_key=status_key,
            body=body,
            wa_message_id=wa_id,
            success=ok,
            error=result.get("error") if not ok else None,
        )
    )
    if not ok:
        log.warning(
            "Customer WhatsApp failed (%s → %s): %s",
            message_type,
            phone,
            result.get("error"),
        )
    return result


async def notify_customer_status_change(
    db: Session,
    shipment: Shipment,
    new_status: str,
    *,
    old_status: str | None = None,
) -> dict:
    """
    Send the status-specific WhatsApp to the customer if enabled and not yet sent.
    On delivered, also open the feedback conversation.
    """
    if not settings.CUSTOMER_WHATSAPP_ENABLED:
        return {"skipped": True, "reason": "disabled"}

    status = (new_status or "").strip().lower()
    if status not in CUSTOMER_NOTIFY_STATUSES:
        return {"skipped": True, "reason": f"status '{status}' not customer-facing"}

    if old_status and old_status == status:
        return {"skipped": True, "reason": "no change"}

    order = shipment.order
    if order is None and shipment.order_id:
        order = db.query(Order).filter(Order.id == shipment.order_id).first()
    if not order:
        return {"skipped": True, "reason": "order missing"}

    phone = normalize_phone(order.customer_phone)
    if not phone:
        return {"skipped": True, "reason": "no valid customer phone"}

    message_type = f"status_{status}"
    if _already_sent(db, shipment.id, message_type):
        return {"skipped": True, "reason": "already sent", "message_type": message_type}

    body = build_status_message(status, order, shipment)
    if not body:
        return {"skipped": True, "reason": "no template"}

    result = await _send_and_log(
        db,
        phone=phone,
        body=body,
        message_type=message_type,
        shipment=shipment,
        order=order,
        status_key=status,
    )

    if "error" not in result and status == "delivered":
        conv = _get_or_create_conversation(db, shipment, order, phone)
        conv.state = "awaiting_feedback"
        conv.updated_at = datetime.utcnow()
        # Also log a logical feedback_request marker (same outbound body)
        if not _already_sent(db, shipment.id, "feedback_request"):
            db.add(
                CustomerMessageLog(
                    shipment_id=shipment.id,
                    order_id=order.id,
                    phone=phone,
                    direction="outbound",
                    message_type="feedback_request",
                    status_key="delivered",
                    body=body,
                    success=True,
                )
            )

    db.add(
        AlertLog(
            shipment_id=shipment.id,
            alert_type=f"customer_{message_type}",
            channel="whatsapp",
            recipient=phone,
            message_preview=(body or "")[:500],
        )
    )
    return {
        "sent": "error" not in result,
        "phone": phone,
        "message_type": message_type,
        "status": status,
        "result": result,
    }


async def send_followup_after_feedback(
    db: Session, conversation: CustomerConversation
) -> dict:
    """After customer leaves feedback, ask if they need anything else."""
    if not settings.CUSTOMER_WHATSAPP_ENABLED:
        return {"skipped": True, "reason": "disabled"}

    if conversation.state == "followup_sent" or conversation.followup_sent_at:
        return {"skipped": True, "reason": "followup already sent"}

    shipment = conversation.shipment
    if shipment is None:
        shipment = (
            db.query(Shipment).filter(Shipment.id == conversation.shipment_id).first()
        )
    order = shipment.order if shipment else None
    if order is None and conversation.order_id:
        order = db.query(Order).filter(Order.id == conversation.order_id).first()
    if not order or not shipment:
        return {"skipped": True, "reason": "order/shipment missing"}

    phone = normalize_phone(conversation.phone or order.customer_phone)
    if not phone:
        return {"skipped": True, "reason": "no phone"}

    if _already_sent(db, shipment.id, "followup"):
        conversation.state = "followup_sent"
        conversation.followup_sent_at = conversation.followup_sent_at or datetime.utcnow()
        return {"skipped": True, "reason": "already sent"}

    body = build_followup_message(order)
    result = await _send_and_log(
        db,
        phone=phone,
        body=body,
        message_type="followup",
        shipment=shipment,
        order=order,
        status_key="delivered",
    )
    if "error" not in result:
        conversation.state = "followup_sent"
        conversation.followup_sent_at = datetime.utcnow()
        conversation.updated_at = datetime.utcnow()
    return {"sent": "error" not in result, "result": result}


def _find_conversation_for_phone(db: Session, phone: str) -> CustomerConversation | None:
    """Prefer open feedback conversations for this phone (most recent first)."""
    normalized = normalize_phone(phone)
    if not normalized:
        return None

    # Match exact normalized phone, or last-10-digit overlap for messy storage
    last10 = normalized[-10:]
    open_states = ("awaiting_feedback", "feedback_received", "followup_sent")
    candidates = (
        db.query(CustomerConversation)
        .filter(CustomerConversation.state.in_(open_states))
        .order_by(CustomerConversation.updated_at.desc())
        .limit(50)
        .all()
    )
    for conv in candidates:
        cphone = normalize_phone(conv.phone) or ""
        if cphone == normalized or cphone.endswith(last10) or normalized.endswith(cphone[-10:]):
            return conv

    # Fallback: any conversation for this phone
    all_convs = (
        db.query(CustomerConversation)
        .order_by(CustomerConversation.updated_at.desc())
        .limit(100)
        .all()
    )
    for conv in all_convs:
        cphone = normalize_phone(conv.phone) or ""
        if cphone == normalized or (cphone and cphone.endswith(last10)):
            return conv
    return None


async def handle_inbound_customer_message(
    db: Session,
    from_phone: str,
    text: str,
    *,
    wa_message_id: str | None = None,
) -> dict:
    """
    Process a customer WhatsApp reply.

    If they were awaiting feedback → store review + send follow-up.
    Otherwise log the inbound message for the team.
    """
    phone = normalize_phone(from_phone)
    body = (text or "").strip()
    if not phone or not body:
        return {"ok": False, "reason": "empty phone or body"}

    conv = _find_conversation_for_phone(db, phone)

    db.add(
        CustomerMessageLog(
            shipment_id=conv.shipment_id if conv else None,
            order_id=conv.order_id if conv else None,
            phone=phone,
            direction="inbound",
            message_type="inbound_reply",
            status_key=conv.state if conv else None,
            body=body,
            wa_message_id=wa_message_id,
            success=True,
        )
    )

    if not conv:
        # Notify team about unmatched customer message
        if settings.CUSTOMER_FORWARD_UNMATCHED_TO_TEAM:
            team_msg = (
                f"📩 Customer WhatsApp (no open order chat)\n"
                f"From: {phone}\n"
                f"Message: {body[:800]}"
            )
            await whatsapp.broadcast(team_msg)
        return {"ok": True, "matched": False}

    conv.last_inbound_at = datetime.utcnow()
    conv.updated_at = datetime.utcnow()

    if conv.state == "awaiting_feedback":
        conv.feedback_text = body
        conv.feedback_received_at = datetime.utcnow()
        conv.state = "feedback_received"

        # Notify team of the review
        order = None
        if conv.order_id:
            order = db.query(Order).filter(Order.id == conv.order_id).first()
        shipment = (
            db.query(Shipment).filter(Shipment.id == conv.shipment_id).first()
        )
        team_msg = (
            f"⭐ Customer feedback received\n"
            f"Order: {order.order_number if order else '—'}\n"
            f"CN: {shipment.tracking_number if shipment else '—'}\n"
            f"Customer: {order.customer_name if order else phone} ({phone})\n"
            f"Feedback: {body[:800]}"
        )
        await whatsapp.broadcast(team_msg)
        db.add(
            AlertLog(
                shipment_id=conv.shipment_id,
                alert_type="customer_feedback",
                channel="whatsapp",
                recipient=phone,
                message_preview=body[:500],
            )
        )

        follow = await send_followup_after_feedback(db, conv)
        return {
            "ok": True,
            "matched": True,
            "action": "feedback_saved",
            "followup": follow,
            "shipment_id": conv.shipment_id,
        }

    # Already past feedback — treat as general help request, forward to team
    order = None
    if conv.order_id:
        order = db.query(Order).filter(Order.id == conv.order_id).first()
    shipment = db.query(Shipment).filter(Shipment.id == conv.shipment_id).first()
    team_msg = (
        f"💬 Customer reply\n"
        f"Order: {order.order_number if order else '—'}\n"
        f"CN: {shipment.tracking_number if shipment else '—'}\n"
        f"State: {conv.state}\n"
        f"From: {phone}\n"
        f"Message: {body[:800]}"
    )
    await whatsapp.broadcast(team_msg)
    if conv.state == "followup_sent":
        conv.state = "closed"
    return {
        "ok": True,
        "matched": True,
        "action": "forwarded_to_team",
        "shipment_id": conv.shipment_id,
    }
