"""Registry of all courier clients. Add new couriers here."""
from app.services.couriers.base import BaseCourier
from app.services.couriers.postex import PostExCourier
from app.services.couriers.daewoo import DaewooCourier
from app.services.couriers.digidokaan import DigiDokaanCourier
from app.services.couriers.leopards import LeopardsCourier


_REGISTRY: dict[str, BaseCourier] = {
    "postex":     PostExCourier(),
    "daewoo":     DaewooCourier(),
    "digidokaan": DigiDokaanCourier(),
    "leopards":   LeopardsCourier(),
}


def get_courier(name: str) -> BaseCourier | None:
    return _REGISTRY.get(name.lower())


def all_couriers() -> list[BaseCourier]:
    return list(_REGISTRY.values())
