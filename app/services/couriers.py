"""Unified courier dispatcher."""
from app.services import postex, daewoo, digidokaan


COURIER_HANDLERS = {
    "postex": postex,
    "daewoo": daewoo,
    "digidokaan": digidokaan,
}


async def track_shipment(courier: str, tracking_number: str) -> dict:
    handler = COURIER_HANDLERS.get((courier or "").lower())
    if not handler:
        return {
            "error": f"No handler for courier '{courier}'. "
                     f"Available: {list(COURIER_HANDLERS.keys())}"
        }
    return await handler.track(tracking_number)
