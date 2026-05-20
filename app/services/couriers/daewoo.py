"""
Daewoo Fastex courier client.

You already have this integrated in BizHisaab; we just call the same
endpoints from this standalone service.
TODO: Replace stub status map with actual Daewoo response codes.
"""
from datetime import datetime
import httpx
from app.config import settings
from app.models import ShipmentStatus
from app.services.couriers.base import BaseCourier, NormalizedStatus


DAEWOO_STATUS_MAP = {
    "BOOKED":        ShipmentStatus.BOOKED,
    "PICKED":        ShipmentStatus.ARRIVED_WAREHOUSE,
    "INTRANSIT":     ShipmentStatus.IN_TRANSIT,
    "OFD":           ShipmentStatus.OUT_FOR_DELIVERY,
    "DELIVERED":     ShipmentStatus.DELIVERED,
    "RTO_IN_PROCESS": ShipmentStatus.RETURN_IN_PROCESS,
    "RTO":           ShipmentStatus.RETURN_TO_SHIPPER,
    "RETURNED":      ShipmentStatus.RETURNED,
}


class DaewooCourier(BaseCourier):
    name = "daewoo"

    def __init__(self):
        self.base_url = settings.daewoo_api_url
        self.headers = {
            "Authorization": f"Bearer {settings.daewoo_api_key}",
            "Content-Type": "application/json",
        }

    async def track(self, tracking_number: str) -> NormalizedStatus:
        url = f"{self.base_url}/track/{tracking_number}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()
            data = r.json()

        raw_status = (data.get("status") or "").upper()
        normalized = DAEWOO_STATUS_MAP.get(raw_status, ShipmentStatus.IN_TRANSIT)

        return NormalizedStatus(
            status=normalized,
            note=data.get("statusText", raw_status),
            location=data.get("currentLocation", ""),
            raw=data,
        )

    async def fetch_payouts(self, since: datetime) -> list[dict]:
        """Daewoo payout API — adjust to actual response shape."""
        return []
