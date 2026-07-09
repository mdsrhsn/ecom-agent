"""Webhooks + agent chat API."""
import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, HTTPException, Depends, Query, Response
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import Order, Shipment, StatusEvent
from app.services.shopify import verify_webhook, parse_order_payload, detect_courier
from app.services.customer_messaging import (
    notify_customer_status_change,
    handle_inbound_customer_message,
)
from app.agent.claude_client import chat as agent_chat
from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/shopify/orders-create")
async def shopify_order_created(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")

    if not verify_webhook(body, hmac_header):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    parsed = parse_order_payload(payload)

    if db.query(Order).filter(Order.shopify_order_id == parsed["shopify_order_id"]).first():
        return {"status": "duplicate"}

    order = Order(**parsed)
    db.add(order)
    db.flush()

    for f in payload.get("fulfillments", []):
        tn = f.get("tracking_number")
        if not tn:
            continue
        courier = detect_courier(parsed["shopify_tags"], tn)
        sh = Shipment(
            order_id=order.id,
            courier=courier,
            tracking_number=tn,
            pcs_count=parsed["items_count"],
            cod_amount=parsed["cod_amount"],
            current_status="booked",
        )
        db.add(sh)
        db.flush()
        db.add(StatusEvent(
            shipment_id=sh.id,
            status="booked",
            raw_status="booked",
            description="Booked via Shopify order create",
        ))
        await notify_customer_status_change(db, sh, "booked", old_status=None)
    db.commit()
    db.refresh(order)
    return {"status": "ok", "order_id": order.id}


@router.post("/webhooks/shopify/fulfillment-create")
async def shopify_fulfillment_created(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if not verify_webhook(body, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        raise HTTPException(status_code=401)

    payload = await request.json()
    order_id = str(payload.get("order_id"))
    tracking_number = payload.get("tracking_number")
    if not tracking_number:
        return {"status": "no tracking number"}

    order = db.query(Order).filter(Order.shopify_order_id == order_id).first()
    if not order:
        return {"status": "order not found"}

    if db.query(Shipment).filter(Shipment.tracking_number == tracking_number).first():
        return {"status": "shipment already exists"}

    courier = detect_courier(order.shopify_tags, tracking_number)
    sh = Shipment(
        order_id=order.id,
        courier=courier,
        tracking_number=tracking_number,
        pcs_count=order.items_count,
        cod_amount=order.cod_amount,
        current_status="booked",
    )
    db.add(sh)
    db.flush()
    db.add(StatusEvent(
        shipment_id=sh.id,
        status="booked",
        raw_status="booked",
        description="Booked via Shopify fulfillment",
    ))
    customer_notify = await notify_customer_status_change(
        db, sh, "booked", old_status=None
    )
    db.commit()
    return {
        "status": "ok",
        "shipment_id": sh.id,
        "courier": courier,
        "customer_notify": customer_notify,
    }


@router.post("/agent/chat")
async def chat_with_agent(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    user_message = (body.get("message") or "").strip()
    history = body.get("history", [])
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")

    # History from the browser is plain text; convert to simple format.
    simple_history = []
    for h in history:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            simple_history.append({"role": h["role"], "content": h["content"]})

    result = await agent_chat(user_message, db, history=simple_history)
    return {"reply": result["reply"], "tool_calls": result["tool_calls"]}


def _verify_whatsapp_signature(body: bytes, signature_header: str | None) -> bool:
    """Optional Meta X-Hub-Signature-256 check when WHATSAPP_APP_SECRET is set."""
    secret = settings.WHATSAPP_APP_SECRET
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


@router.get("/webhooks/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN
    ):
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="WhatsApp verify failed")


@router.post("/webhooks/whatsapp")
async def whatsapp_inbound(request: Request, db: Session = Depends(get_db)):
    """
    Receive customer WhatsApp replies (feedback + follow-up help).
    Configure this URL in Meta Developer Console → WhatsApp → Configuration.
    """
    raw = await request.body()
    if not _verify_whatsapp_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="Invalid WhatsApp signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    processed = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            for msg in value.get("messages", []) or []:
                if msg.get("type") != "text":
                    continue
                from_phone = msg.get("from", "")
                text = (msg.get("text") or {}).get("body", "")
                wa_id = msg.get("id")
                try:
                    result = await handle_inbound_customer_message(
                        db, from_phone, text, wa_message_id=wa_id
                    )
                    processed.append(result)
                except Exception as e:
                    log.exception("Inbound WhatsApp handler failed: %s", e)
                    processed.append({"ok": False, "error": str(e)})

    db.commit()
    return {"status": "ok", "processed": len(processed), "results": processed}
