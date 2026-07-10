"""
Shopify integration: webhook verification, courier detection from tags + tracking prefix.
"""
import hmac
import hashlib
import base64
import httpx
from datetime import datetime
from app.config import settings


def verify_webhook(body_bytes: bytes, hmac_header: str) -> bool:
    if not settings.SHOPIFY_WEBHOOK_SECRET or not hmac_header:
        return False
    digest = hmac.new(
        settings.SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header)


# Tracking number prefixes (most reliable courier hint)
TRACKING_PREFIXES = {
    "PX": "postex",
    "POSTEX": "postex",
    "DW": "daewoo",
    "DAE": "daewoo",
    "DD": "digidokaan",
    "DGD": "digidokaan",
    "LP": "leopards",
    "LCS": "leopards",
    "TCS": "tcs",
}

# Shopify tag values (manual hint)
TAG_MAP = {
    "postex": "postex",
    "postx": "postex",
    "daewoo": "daewoo",
    "dewoo": "daewoo",
    "digidokaan": "digidokaan",
    "digidukaan": "digidokaan",
    "leopards": "leopards",
    "lcs": "leopards",
    "tcs": "tcs",
}


def detect_courier_from_tracking(tracking_number: str):
    if not tracking_number:
        return None
    tn = tracking_number.upper().strip()
    for prefix, courier in TRACKING_PREFIXES.items():
        if tn.startswith(prefix):
            return courier
    return None


def detect_courier_from_tags(tags: str):
    if not tags:
        return None
    for tag in tags.lower().split(","):
        tag = tag.strip()
        if tag in TAG_MAP:
            return TAG_MAP[tag]
    return None


def detect_courier(tags: str, tracking_number: str = None) -> str:
    """Tracking prefix wins, then tag, else 'unknown'."""
    return (
        detect_courier_from_tracking(tracking_number or "")
        or detect_courier_from_tags(tags or "")
        or "unknown"
    )


def _api_url(path: str) -> str:
    return f"https://{settings.SHOPIFY_STORE_URL}/admin/api/2024-10/{path}"


def _headers() -> dict:
    return {
        "X-Shopify-Access-Token": settings.SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


async def get_order(order_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_api_url(f"orders/{order_id}.json"), headers=_headers())
        r.raise_for_status()
        return r.json().get("order", {})


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_fulfillment_payload(payload: dict) -> dict:
    """
    Build a minimal Order dict from a *fulfillment* webhook payload.

    Used when a fulfillment arrives for an order we never received an
    orders/create webhook for (e.g. old orders booked after the app went live).
    The fulfillment payload already carries destination + line items + the
    courier name, so we can create the order WITHOUT calling the Shopify API
    (no access token needed).
    """
    dest = payload.get("destination") or {}
    line_items = payload.get("line_items", [])
    pcs = sum(int(li.get("quantity", 0) or 0) for li in line_items)
    # COD proxy = sum(price * qty) of fulfilled line items (exact amount later
    # comes from the courier settlement during payment ingestion).
    cod = 0.0
    for li in line_items:
        try:
            cod += float(li.get("price") or 0) * int(li.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            pass

    name = dest.get("name") or (
        f"{dest.get('first_name', '')} {dest.get('last_name', '')}".strip()
    )
    tags = payload.get("tags", "")  # fulfillment payloads rarely carry tags
    tracking_company = payload.get("tracking_company", "")

    return {
        "shopify_order_id": str(payload.get("order_id")),
        "order_number": str(payload.get("name", "")),  # e.g. "#WC6686.1"
        "customer_name": name,
        "customer_phone": dest.get("phone", "") or "",
        "customer_address": dest.get("address1", "") or "",
        "city": (dest.get("city") or "").strip(),
        "province": dest.get("province", "") or "",
        "total_amount": cod,
        "cod_amount": cod,
        "items_count": pcs,
        "shopify_tags": tags,
        # tracking_company ("PostEx") is the reliable courier hint here:
        "courier_hint": detect_courier_from_tags(f"{tags},{tracking_company}") or "unknown",
        "created_at": _parse_iso(payload.get("created_at")) or datetime.utcnow(),
    }


def parse_order_payload(payload: dict) -> dict:
    shipping = payload.get("shipping_address") or payload.get("billing_address") or {}
    line_items = payload.get("line_items", [])
    pcs = sum(int(li.get("quantity", 0)) for li in line_items)
    tags = payload.get("tags", "")

    return {
        "shopify_order_id": str(payload.get("id")),
        "order_number": payload.get("name", ""),
        "customer_name": (
            f"{shipping.get('first_name', '')} {shipping.get('last_name', '')}".strip()
            or payload.get("email", "")
        ),
        "customer_phone": shipping.get("phone") or payload.get("phone", ""),
        "customer_address": shipping.get("address1", ""),
        "city": (shipping.get("city") or "").strip(),
        "province": shipping.get("province", ""),
        "total_amount": float(payload.get("total_price") or 0),
        "cod_amount": float(payload.get("total_price") or 0),
        "items_count": pcs,
        "shopify_tags": tags,
        "courier_hint": detect_courier_from_tags(tags) or "unknown",
        "created_at": _parse_iso(payload.get("created_at")) or datetime.utcnow(),
    }
