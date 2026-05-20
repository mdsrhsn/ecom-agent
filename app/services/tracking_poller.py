"""
Tracking poller — har 3 ghantay baad chalta hai.

For every active shipment:
  1. Call the courier API
  2. If status changed → append StatusEvent + update shipment
  3. If 3+ days since booking AND not delivered → flag critical
  4. Trigger appropriate notifications
"""
from datetime import datetime, timedelta
import logging
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Shipment, StatusEvent, ShipmentStatus
from app.services.couriers.registry import get_courier
from app.services.notifier import send_critical_alert, send_payment_overdue_alert
from app.config import settings

log = logging.getLogger(__name__)


# Statuses considered "still in courier hands" — these are polled
ACTIVE_STATUSES = {
    ShipmentStatus.BOOKED.value,
    ShipmentStatus.ARRIVED_WAREHOUSE.value,
    ShipmentStatus.IN_TRANSIT.value,
    ShipmentStatus.OUT_FOR_DELIVERY.value,
    ShipmentStatus.RETURN_IN_PROCESS.value,
    ShipmentStatus.RETURN_TO_SHIPPER.value,
}

# Statuses considered "delivered" — start payment timer
FINAL_DELIVERED = {ShipmentStatus.DELIVERED.value}


async def poll_all_active_shipments():
    db: Session = SessionLocal()
    try:
        actives = db.query(Shipment).filter(Shipment.status.in_(ACTIVE_STATUSES)).all()
        log.info("Polling %d active shipments", len(actives))

        for sh in actives:
            courier = get_courier(sh.courier)
            if not courier:
                continue
            try:
                result = await courier.track(sh.tracking_number)
            except Exception as e:
                log.warning("Track failed for %s: %s", sh.tracking_number, e)
                continue

            sh.last_polled_at = datetime.utcnow()
            sh.raw_courier_response = result.raw or {}

            new_status = result.status.value
            if new_status != sh.status:
                # Status change — log it
                event = StatusEvent(
                    shipment_id=sh.id,
                    status=new_status,
                    note=result.note,
                    occurred_at=result.occurred_at or datetime.utcnow(),
                    source="poller",
                )
                db.add(event)
                sh.status = new_status

                # Stamp the relevant timestamp
                now = datetime.utcnow()
                if new_status == ShipmentStatus.ARRIVED_WAREHOUSE.value and not sh.arrived_warehouse_at:
                    sh.arrived_warehouse_at = now
                elif new_status == ShipmentStatus.DELIVERED.value and not sh.delivered_at:
                    sh.delivered_at = now
                elif new_status == ShipmentStatus.RETURNED.value and not sh.returned_at:
                    sh.returned_at = now

            # Critical check — booked 3+ days ago AND not delivered/returned
            if (
                sh.booked_at
                and not sh.delivered_at
                and sh.status not in FINAL_DELIVERED
                and (datetime.utcnow() - sh.booked_at) > timedelta(days=settings.critical_days)
                and not sh.is_critical
            ):
                sh.is_critical = True
                sh.critical_flagged_at = datetime.utcnow()
                await send_critical_alert(db, sh)

        db.commit()
    finally:
        db.close()


async def check_payment_overdue():
    """
    Run daily. Find delivered shipments where >7 days passed and no
    payment recorded — alert.
    """
    db: Session = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=settings.payment_overdue_days)
        shipments = (
            db.query(Shipment)
            .filter(
                Shipment.status == ShipmentStatus.DELIVERED.value,
                Shipment.delivered_at < cutoff,
            )
            .all()
        )
        overdue = [s for s in shipments if not s.payments]
        log.info("Found %d payment-overdue shipments", len(overdue))

        for sh in overdue:
            await send_payment_overdue_alert(db, sh)

        db.commit()
    finally:
        db.close()
