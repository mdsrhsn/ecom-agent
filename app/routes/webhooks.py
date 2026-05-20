"""
Shopify webhook handlers.

We listen for:
  - orders/create  → new Order record
  - orders/updated → update tags, status
  - fulfillments/create → new Shipment record (this is when courier was booked)
"""
import hmac
import hashlib
import base64
import json
from datetime import datetime
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.models import Order, Shipment, StatusEvent, ShipmentStatus
from app.utils.courier_detect import detect_courier

router = APIRouter(prefix="/webhooks/shopify", tags=["webhooks"])


def _verify_hmac(body: bytes, hmac_header: str) -> bool:
    """Validate Shopify webhook signature."""
    if not settings.shopify_webhook_secret:
        return True  # skip in dev
    digest = hmac.new(
        settings.shopify_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, hmac_header or "")


@router.post("/orders")
async def shopify_order_created(
    request: Request,
    db: Session = Depends(get_db),
    x_shopify_hmac_sha256: str = Header(None),
):
    body = await request.body()
    if not _verify_hmac(body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Bad HMAC")

    data = json.loads(body)
    shopify_id = str(data["id"])

    existing = db.query(Order).filter_by(shopify_order_id=shopify_id).first()
    if existing:
        # update tags / fulfillment status
        existing.shopify_tags = data.get("tags", "")
        db.commit()
        return {"ok": True, "action": "updated"}

    ship_addr = data.get("shipping_address") or {}
    items = data.get("line_items", [])

    order = Order(
        shopify_order_id=shopify_id,
        order_number=data.get("name", ""),
        customer_name=f"{ship_addr.get('first_name','')} {ship_addr.get('last_name','')}".strip(),
        customer_phone=ship_addr.get("phone") or data.get("phone") or "",
        customer_address=ship_addr.get("address1", ""),
        city=ship_addr.get("city", ""),
        province=ship_addr.get("province", ""),
        cod_amount=float(data.get("total_price", 0)),
        items_count=sum(int(i.get("quantity", 1)) for i in items),
        items_json=[
            {
                "sku": i.get("sku"),
                "title": i.get("title"),
                "qty": i.get("quantity"),
                "price": i.get("price"),
            }
            for i in items
        ],
        shopify_tags=data.get("tags", ""),
    )
    db.add(order)
    db.commit()
    return {"ok": True, "action": "created", "order_id": order.id}


@router.post("/fulfillments")
async def shopify_fulfillment_created(
    request: Request,
    db: Session = Depends(get_db),
    x_shopify_hmac_sha256: str = Header(None),
):
    """Fired when YOU mark the order as fulfilled / add tracking in Shopify."""
    body = await request.body()
    if not _verify_hmac(body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Bad HMAC")

    data = json.loads(body)
    order_shopify_id = str(data["order_id"])
    order = db.query(Order).filter_by(shopify_order_id=order_shopify_id).first()
    if not order:
        return {"ok": False, "error": "Order not found"}

    tracking_number = (
        data.get("tracking_number")
        or (data.get("tracking_numbers") or [None])[0]
    )
    if not tracking_number:
        return {"ok": False, "error": "No tracking number"}

    courier = detect_courier(
        tracking_number=tracking_number,
        tags=order.shopify_tags or "",
    )
    if not courier:
        # Best-effort: use the company name Shopify sent
        courier = (data.get("tracking_company") or "unknown").lower()

    # Avoid duplicates
    existing = db.query(Shipment).filter_by(tracking_number=tracking_number).first()
    if existing:
        return {"ok": True, "action": "exists", "shipment_id": existing.id}

    shipment = Shipment(
        order_id=order.id,
        tracking_number=tracking_number,
        courier=courier,
        status=ShipmentStatus.BOOKED.value,
        pieces=order.items_count,
        booked_at=datetime.utcnow(),
    )
    db.add(shipment)
    db.flush()
    db.add(StatusEvent(
        shipment_id=shipment.id,
        status=ShipmentStatus.BOOKED.value,
        note="Booked via Shopify fulfillment",
        source="webhook",
    ))
    db.commit()
    return {"ok": True, "action": "created", "shipment_id": shipment.id}
