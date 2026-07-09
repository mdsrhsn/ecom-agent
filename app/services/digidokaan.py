"""DigiDokaan tracking."""
import httpx
from app.config import settings


DD_STATUS_MAP = {
    "Order Booked": "booked",
    "Pickup Done": "arrived_warehouse",
    "At Warehouse": "arrived_warehouse",
    "In Transit": "in_transit",
    "Dispatched": "in_transit",
    "Out For Delivery": "out_for_delivery",
    "Out for Delivery": "out_for_delivery",
    "Delivered": "delivered",
    "Hold": "return_in_process",
    "Reattempt": "return_in_process",
    "Return In Process": "return_in_process",
    "Return To Shipper": "return_to_shipper",
    "Return Received": "received_back",
}


def normalize_status(raw: str) -> str:
    if not raw:
        return "unknown"
    if raw in DD_STATUS_MAP:
        return DD_STATUS_MAP[raw]
    lower = raw.lower()
    for k, v in DD_STATUS_MAP.items():
        if k.lower() == lower:
            return v
    if "return to shipper" in lower:
        return "return_to_shipper"
    if "return" in lower:
        return "return_in_process"
    if "out for delivery" in lower or "out-for-delivery" in lower:
        return "out_for_delivery"
    if "deliver" in lower and "out" not in lower:
        return "delivered"
    if "transit" in lower or "dispatch" in lower:
        return "in_transit"
    return "unknown"


async def track(tracking_number: str) -> dict:
    if not settings.DIGIDOKAAN_API_KEY:
        return {"error": "DigiDokaan API key not configured"}
    url = f"{settings.DIGIDOKAAN_BASE_URL}/orders/{tracking_number}/status"
    headers = {"X-API-Key": settings.DIGIDOKAAN_API_KEY}
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return {"error": f"DigiDokaan tracking failed: {e}"}

    raw_status = data.get("currentStatus", "")
    return {
        "raw_status": raw_status,
        "normalized_status": normalize_status(raw_status),
        "description": data.get("lastRemark", ""),
        "events": data.get("statusHistory", []),
    }
