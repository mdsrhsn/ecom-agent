"""Leopards courier client stub."""
from datetime import datetime
from app.services.couriers.base import BaseCourier, NormalizedStatus
from app.models import ShipmentStatus


class LeopardsCourier(BaseCourier):
    name = "leopards"

    async def track(self, tracking_number: str) -> NormalizedStatus:
        return NormalizedStatus(status=ShipmentStatus.IN_TRANSIT, note="stub")

    async def fetch_payouts(self, since: datetime) -> list[dict]:
        return []
