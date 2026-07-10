"""Webhooks + agent chat API."""
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session
from app.auth import require_key
from app.db.session import get_db
from app.db.models import Order, Shipment
from app.services.shopify import (
    verify_webhook,
    parse_order_payload,
    parse_fulfillment_payload,
    detect_courier,
)
from app.agent.claude_client import chat as agent_chat

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
    db.commit()
    db.refresh(order)

    for f in payload.get("fulfillments", []):
        tn = f.get("tracking_number")
        if not tn:
            continue
        courier = detect_courier(parsed["shopify_tags"], tn)
        db.add(Shipment(
            order_id=order.id,
            courier=courier,
            tracking_number=tn,
            pcs_count=parsed["items_count"],
            cod_amount=parsed["cod_amount"],
            current_status="booked",
        ))
    db.commit()
    return {"status": "ok", "order_id": order.id}


@router.post("/webhooks/shopify/fulfillment-create")
async def shopify_fulfillment_created(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if not verify_webhook(body, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        raise HTTPException(status_code=401)

    payload = await request.json()
    order_id = str(payload.get("order_id"))
    tracking_number = (
        payload.get("tracking_number")
        or (payload.get("tracking_numbers") or [None])[0]
    )
    if not tracking_number:
        return {"status": "no tracking number"}

    order = db.query(Order).filter(Order.shopify_order_id == order_id).first()

    # If we never saw this order (e.g. an OLD order booked after the app went
    # live), build it from the fulfillment payload itself — no Shopify API /
    # access token needed. This makes every booked parcel trackable.
    created_order = False
    if not order:
        order_data = parse_fulfillment_payload(payload)
        order = Order(**order_data)
        db.add(order)
        db.commit()
        db.refresh(order)
        created_order = True

    if db.query(Shipment).filter(Shipment.tracking_number == tracking_number).first():
        return {"status": "shipment already exists"}

    # tracking_company (e.g. "PostEx") is the most reliable courier hint on a
    # fulfillment, since the numeric tracking number has no courier prefix.
    tracking_company = payload.get("tracking_company", "")
    courier = detect_courier(
        f"{order.shopify_tags or ''},{tracking_company}", tracking_number
    )
    sh = Shipment(
        order_id=order.id,
        courier=courier,
        tracking_number=tracking_number,
        pcs_count=order.items_count,
        cod_amount=order.cod_amount,
        current_status="booked",
    )
    db.add(sh)
    db.commit()
    return {
        "status": "ok",
        "shipment_id": sh.id,
        "courier": courier,
        "order_created_from_fulfillment": created_order,
    }


@router.post("/agent/chat")
async def chat_with_agent(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_key),
):
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
