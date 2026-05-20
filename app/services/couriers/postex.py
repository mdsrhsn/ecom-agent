"""
PostEx courier API wrapper.

Docs: https://postex.pk → Merchant Portal → API
Notes:
  - PostEx returns transactionStatus strings; we map them to ShipmentStatus
  - Auth header: 'token' (NOT 'Authorization')
  - IMPORTANT: PostEx ke actual status strings ko verify karna padega
    real responses se. Yahan main ne educated guesses use ki hain
    standard PostEx merchant docs ke base pe.
"""
from datetime import datetime
import httpx
from app.config import settings
from app.models import ShipmentStatus
from app.services.couriers.base import BaseCourier, NormalizedStatus


# PostEx status strings → our canonical enum
# Verify these against actual responses; adjust as needed.
POSTEX_STATUS_MAP = {
    "Booked":                ShipmentStatus.BOOKED,
    "PickedUp":              ShipmentStatus.ARRIVED_WAREHOUSE,
    "Arrived at Warehouse":  ShipmentStatus.ARRIVED_WAREHOUSE,
    "In Transit":            ShipmentStatus.IN_TRANSIT,
    "Out for Delivery":      ShipmentStatus.OUT_FOR_DELIVERY,
    "Delivered":             ShipmentStatus.DELIVERED,
    "Return in Process":     ShipmentStatus.RETURN_IN_PROCESS,
    "Return to Shipper":     ShipmentStatus.RETURN_TO_SHIPPER,
    "Returned":              ShipmentStatus.RETURNED,
    "Lost":                  ShipmentStatus.LOST,
    "Cancelled":             ShipmentStatus.CANCELLED,
}


class PostExCourier(BaseCourier):
    name = "postex"

    def __init__(self):
        self.base_url = settings.postex_api_url
        self.headers = {
            "token": settings.postex_api_key,
            "Content-Type": "application/json",
        }

    async def track(self, tracking_number: str) -> NormalizedStatus:
        url = f"{self.base_url}/track-order/{tracking_number}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()
            data = r.json()

        # PostEx wraps payload in {"statusCode":"200","dist":[{...}]}
        dist = data.get("dist", [])
        latest = dist[-1] if dist else data

        raw_status = latest.get("transactionStatusMessage") or latest.get("status", "")
        normalized = POSTEX_STATUS_MAP.get(raw_status, ShipmentStatus.IN_TRANSIT)

        return NormalizedStatus(
            status=normalized,
            note=raw_status,
            location=latest.get("modifiedDatetime", ""),
            occurred_at=self._parse_dt(latest.get("modifiedDatetime")),
            raw=data,
        )

    async def fetch_payouts(self, since: datetime) -> list[dict]:
        """
        PostEx pays out via 'transactions' endpoint. Adjust based on
        your real merchant API access.
        """
        url = f"{self.base_url}/get-merchant-transaction-history"
        params = {"fromDate": since.strftime("%Y-%m-%d")}
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=self.headers, params=params)
            r.raise_for_status()
            data = r.json()

        payouts = []
        for tx in data.get("dist", []):
            # Each transaction usually has multiple shipments
            for sh in tx.get("shipments", []):
                payouts.append({
                    "tracking_number": sh.get("trackingNumber"),
                    "amount": float(sh.get("amount", 0)),
                    "payout_date": tx.get("transactionDate"),
                    "reference": tx.get("transactionId"),
                })
        return payouts

    @staticmethod
    def _parse_dt(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
