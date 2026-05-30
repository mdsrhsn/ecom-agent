"""
Avix courier tracking. Generic structure — will be customized once we have
the actual Avix API endpoint/response format from Mudassar's docs.
"""
import httpx
from app.config import settings


AVIX_STATUS_MAP = {
    # Common Pakistani courier status patterns; will refine when Avix docs land.
    "booked": "booked",
    "order booked": "booked",
    "picked": "arrived_warehouse",
    "pickup": "arrived_warehouse",
    "received": "arrived_warehouse",
    "arrived at warehouse": "arrived_warehouse",
    "in transit": "in_transit",
    "dispatched": "in_transit",
    "out for delivery": "in_transit",
    "delivered": "delivered",
    "attempted": "return_in_process",
    "customer refused": "return_in_process",
    "return in process": "return_in_process",
    "hold": "return_in_process",
    "returned": "return_to_shipper",
    "return to shipper": "return_to_shipper",
    "rts": "return_to_shipper",
    "received back": "received_back",
    "cancelled": "cancelled",
    "lost": "lost",
}


def normalize_status(raw: str) -> str:
    if not raw:
        return "unknown"
    lower = raw.lower().strip()
    if lower in AVIX_STATUS_MAP:
        return AVIX_STATUS_MAP[lower]
    # Fuzzy matches
    if "return to shipper" in lower or "rts" in lower:
        return "return_to_shipper"
    if "return" in lower and "process" in lower:
        return "return_in_process"
    if "deliver" in lower and "out" not in lower:
        return "delivered"
    if "transit" in lower or "dispatch" in lower:
        return "in_transit"
    if "warehouse" in lower or "picked" in lower or "received" in lower:
        return "arrived_warehouse"
    return "unknown"


async def track(tracking_number: str) -> dict:
    if not settings.AVIX_API_KEY or not settings.AVIX_BASE_URL:
        return {"error": "Avix API key/base URL not configured"}

    # TODO: confirm exact endpoint and header format from Avix docs
    url = f"{settings.AVIX_BASE_URL}/track/{tracking_number}"
    headers = {"Authorization": f"Bearer {settings.AVIX_API_KEY}"}

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return {"error": f"Avix tracking failed: {e}"}

    # TODO: adjust based on actual response shape
    raw_status = data.get("status", "")
    history = data.get("history", [])

    return {
        "raw_status": raw_status,
        "normalized_status": normalize_status(raw_status),
        "description": history[-1].get("description", "") if history else "",
        "events": [
            {
                "raw_status": h.get("status", ""),
                "normalized_status": normalize_status(h.get("status", "")),
                "occurred_at": h.get("timestamp") or h.get("date"),
            }
            for h in history
        ],
    }
