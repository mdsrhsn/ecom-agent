"""Daewoo (FastEx) tracking."""
import httpx
from app.config import settings


DAEWOO_STATUS_MAP = {
    "Booked": "booked",
    "Received": "arrived_warehouse",
    "Arrived at Hub": "arrived_warehouse",
    "In Transit": "in_transit",
    "Dispatched": "in_transit",
    "Out for Delivery": "out_for_delivery",
    "Out For Delivery": "out_for_delivery",
    "Delivered": "delivered",
    "Attempted": "return_in_process",
    "Hold": "return_in_process",
    "Returned to Shipper": "return_to_shipper",
    "Returned": "return_to_shipper",
    "Received Back": "received_back",
    "Cancelled": "cancelled",
}


def normalize_status(raw: str) -> str:
    if not raw:
        return "unknown"
    if raw in DAEWOO_STATUS_MAP:
        return DAEWOO_STATUS_MAP[raw]
    lower = raw.lower()
    for k, v in DAEWOO_STATUS_MAP.items():
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
    if not settings.DAEWOO_API_KEY:
        return {"error": "Daewoo API key not configured"}
    url = f"{settings.DAEWOO_BASE_URL}/track/{tracking_number}"
    headers = {"Authorization": f"Bearer {settings.DAEWOO_API_KEY}"}
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return {"error": f"Daewoo tracking failed: {e}"}

    raw_status = data.get("status", "")
    return {
        "raw_status": raw_status,
        "normalized_status": normalize_status(raw_status),
        "description": data.get("description", ""),
        "events": data.get("history", []),
    }
