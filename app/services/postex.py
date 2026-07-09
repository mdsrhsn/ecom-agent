"""
PostEx tracking. Status mapping critically distinguishes:
  "Return in process" (under decision, may still deliver)
  vs "Return to shipper" (confirmed, coming back to us)
"""
import httpx
from app.config import settings


POSTEX_STATUS_MAP = {
    "Booked": "booked",
    "Order Booked": "booked",
    "Picked": "arrived_warehouse",
    "Pickup": "arrived_warehouse",
    "Arrived at Warehouse": "arrived_warehouse",
    "Received at Warehouse": "arrived_warehouse",
    "Dispatched": "in_transit",
    "In Transit": "in_transit",
    "Out for Delivery": "out_for_delivery",
    "Out For Delivery": "out_for_delivery",
    "Delivered": "delivered",
    "Attempted": "return_in_process",
    "Customer Refused": "return_in_process",
    "Return in Process": "return_in_process",
    "Re-Attempt": "return_in_process",
    "Hold": "return_in_process",
    "Returned": "return_to_shipper",
    "Return to Shipper": "return_to_shipper",
    "RTS": "return_to_shipper",
    "Return Confirmed": "return_to_shipper",
    "Received Back": "received_back",
    "Cancelled": "cancelled",
    "Lost": "lost",
}


def normalize_status(raw: str) -> str:
    if not raw:
        return "unknown"
    if raw in POSTEX_STATUS_MAP:
        return POSTEX_STATUS_MAP[raw]
    lower = raw.lower()
    for key, val in POSTEX_STATUS_MAP.items():
        if key.lower() == lower:
            return val
    if "return to shipper" in lower or "rts" in lower:
        return "return_to_shipper"
    if "return" in lower and "process" in lower:
        return "return_in_process"
    if "out for delivery" in lower or "out-for-delivery" in lower:
        return "out_for_delivery"
    if "deliver" in lower and "out" not in lower:
        return "delivered"
    if "transit" in lower or "dispatch" in lower:
        return "in_transit"
    if "warehouse" in lower or "picked" in lower:
        return "arrived_warehouse"
    return "unknown"


async def track(tracking_number: str) -> dict:
    if not settings.POSTEX_API_KEY:
        return {"error": "PostEx API key not configured"}

    url = f"{settings.POSTEX_BASE_URL}/track-order/{tracking_number}"
    headers = {"token": settings.POSTEX_API_KEY}

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return {"error": f"PostEx tracking failed: {e}"}

    dist = data.get("dist", {})
    raw_status = dist.get("transactionStatus", "")
    history = dist.get("transactionStatusHistory", [])

    latest_desc = history[-1].get("transactionStatusMessage", "") if history else ""

    return {
        "raw_status": raw_status,
        "normalized_status": normalize_status(raw_status),
        "description": latest_desc,
        "events": [
            {
                "raw_status": h.get("transactionStatusMessage", ""),
                "normalized_status": normalize_status(h.get("transactionStatusMessage", "")),
                "occurred_at": h.get("modifiedDatetime"),
            }
            for h in history
        ],
    }
