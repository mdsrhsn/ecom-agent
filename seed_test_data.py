"""
Seed the local SQLite DB with realistic test orders so the dashboard shows
meaningful numbers. Safe to run multiple times — wipes existing test rows first.

Usage:
    python seed_test_data.py
"""
import random
from datetime import datetime, timedelta

from app.db.session import SessionLocal, init_db
from app.db.models import Order, Shipment, StatusEvent, Payment


def wipe_test_data(db):
    """Remove previously-seeded test rows so we start fresh each run."""
    db.query(Payment).delete()
    db.query(StatusEvent).delete()
    db.query(Shipment).delete()
    db.query(Order).filter(Order.shopify_order_id.like("TEST-%")).delete()
    db.commit()


def make_order(db, idx, customer, phone, city, address, total, items, tags, hours_ago):
    order = Order(
        shopify_order_id=f"TEST-{idx:04d}",
        order_number=f"#100{idx}",
        customer_name=customer,
        customer_phone=phone,
        customer_address=address,
        city=city,
        province={"Lahore": "Punjab", "Karachi": "Sindh", "Islamabad": "Capital",
                  "Multan": "Punjab", "Rawalpindi": "Punjab", "Faisalabad": "Punjab"}.get(city, "Punjab"),
        total_amount=total,
        cod_amount=total,
        items_count=items,
        shopify_tags=tags,
        courier_hint=tags,
        created_at=datetime.utcnow() - timedelta(hours=hours_ago),
    )
    db.add(order)
    db.flush()
    return order


def make_shipment(db, order, courier, tracking, pcs, cod, status,
                  booked_hours_ago, arrived_hours_ago=None,
                  delivered_hours_ago=None, returned_hours_ago=None):
    now = datetime.utcnow()
    sh = Shipment(
        order_id=order.id,
        courier=courier,
        tracking_number=tracking,
        pcs_count=pcs,
        cod_amount=cod,
        current_status=status,
        last_status_at=now - timedelta(hours=booked_hours_ago // 2),
        booked_at=now - timedelta(hours=booked_hours_ago),
        arrived_warehouse_at=(now - timedelta(hours=arrived_hours_ago)) if arrived_hours_ago else None,
        delivered_at=(now - timedelta(hours=delivered_hours_ago)) if delivered_hours_ago else None,
        returned_at=(now - timedelta(hours=returned_hours_ago)) if returned_hours_ago else None,
    )
    db.add(sh)
    db.flush()

    # Status event timeline
    db.add(StatusEvent(
        shipment_id=sh.id, status="booked",
        description="Order booked to courier",
        occurred_at=now - timedelta(hours=booked_hours_ago),
    ))
    if arrived_hours_ago:
        db.add(StatusEvent(
            shipment_id=sh.id, status="arrived_warehouse",
            description="Picked up at courier warehouse",
            occurred_at=now - timedelta(hours=arrived_hours_ago),
        ))
    if status in ("in_transit", "delivered", "return_in_process", "return_to_shipper", "received_back"):
        db.add(StatusEvent(
            shipment_id=sh.id, status="in_transit",
            description="Out for delivery",
            occurred_at=now - timedelta(hours=max(2, booked_hours_ago - 24)),
        ))
    if delivered_hours_ago:
        db.add(StatusEvent(
            shipment_id=sh.id, status="delivered",
            description="Delivered to customer",
            occurred_at=now - timedelta(hours=delivered_hours_ago),
        ))
    if status == "return_in_process":
        db.add(StatusEvent(
            shipment_id=sh.id, status="return_in_process",
            description="Customer refused — under return decision",
            occurred_at=now - timedelta(hours=max(1, booked_hours_ago // 3)),
        ))
    if status == "return_to_shipper":
        db.add(StatusEvent(
            shipment_id=sh.id, status="return_to_shipper",
            description="Confirmed return — coming back to shipper",
            occurred_at=now - timedelta(hours=max(1, booked_hours_ago // 4)),
        ))
    if returned_hours_ago:
        db.add(StatusEvent(
            shipment_id=sh.id, status="received_back",
            description="Parcel received back in your warehouse",
            occurred_at=now - timedelta(hours=returned_hours_ago),
        ))

    return sh


def make_payment(db, shipment, amount, hours_ago):
    """COD payment received from courier."""
    db.add(Payment(
        shipment_id=shipment.id,
        amount_received=amount,
        expected_amount=shipment.cod_amount,
        courier_fee=round(amount * 0.02, 2),
        received_at=datetime.utcnow() - timedelta(hours=hours_ago),
    ))


def seed(db):
    """8 realistic orders covering every dashboard state."""

    # ============================================================
    # GROUP A — 3 brand-new orders TODAY (a few hours ago)
    # All booked to courier today, just arrived at warehouse
    # ============================================================
    o = make_order(db, 1, "Ayesha Khan", "03001234567", "Lahore",
                   "House 24, DHA Phase 5, Lahore", 3500, 2, "postex", hours_ago=4)
    make_shipment(db, o, "postex", "PX10001234", pcs=2, cod=3500,
                  status="arrived_warehouse",
                  booked_hours_ago=3, arrived_hours_ago=1)

    o = make_order(db, 2, "Hina Malik", "03219876543", "Lahore",
                   "Flat 7, Gulberg III, Lahore", 2200, 1, "daewoo", hours_ago=5)
    make_shipment(db, o, "daewoo", "DW55678901", pcs=1, cod=2200,
                  status="booked", booked_hours_ago=2)

    o = make_order(db, 3, "Saima Ahmed", "03331122334", "Karachi",
                   "Bungalow 12, Clifton Block 4, Karachi", 4800, 3, "postex", hours_ago=6)
    make_shipment(db, o, "postex", "PX10001235", pcs=3, cod=4800,
                  status="booked", booked_hours_ago=3)

    # ============================================================
    # GROUP B — 1 order delivered TODAY (in last 24h) — happy path
    # ============================================================
    o = make_order(db, 4, "Fatima Bashir", "03451478963", "Islamabad",
                   "Street 11, F-7/3, Islamabad", 2900, 2, "postex", hours_ago=72)
    sh = make_shipment(db, o, "postex", "PX10001100", pcs=2, cod=2900,
                       status="delivered",
                       booked_hours_ago=72, arrived_hours_ago=60, delivered_hours_ago=6)
    # No payment received yet — within 7-day window, this is normal

    # ============================================================
    # GROUP C — 1 CRITICAL: 4 days booked, still not delivered ⚠️
    # ============================================================
    o = make_order(db, 5, "Nida Iqbal", "03007654321", "Multan",
                   "Mohallah Pak, Gulgasht Colony, Multan", 1850, 1, "daewoo", hours_ago=96)
    make_shipment(db, o, "daewoo", "DW55678500", pcs=1, cod=1850,
                  status="in_transit",
                  booked_hours_ago=96, arrived_hours_ago=80)

    # ============================================================
    # GROUP D — 1 PAYMENT OVERDUE: delivered 9 days ago, no payment ⚠️
    # ============================================================
    o = make_order(db, 6, "Maria Sheikh", "03129988776", "Rawalpindi",
                   "House 5, Saddar, Rawalpindi", 3200, 2, "postex", hours_ago=11 * 24)
    make_shipment(db, o, "postex", "PX10000888", pcs=2, cod=3200,
                  status="delivered",
                  booked_hours_ago=11 * 24, arrived_hours_ago=10 * 24,
                  delivered_hours_ago=9 * 24)
    # No payment row — that's the "overdue" condition

    # ============================================================
    # GROUP E — 1 RETURN IN PROCESS (customer refused, still deciding)
    # ============================================================
    o = make_order(db, 7, "Sana Tariq", "03021357924", "Faisalabad",
                   "Plot 88, Jaranwala Road, Faisalabad", 2400, 1, "digidokaan", hours_ago=4 * 24)
    make_shipment(db, o, "digidokaan", "DD77001122", pcs=1, cod=2400,
                  status="return_in_process",
                  booked_hours_ago=4 * 24, arrived_hours_ago=3 * 24)

    # ============================================================
    # GROUP F — 1 RETURN TO SHIPPER (confirmed coming back)
    # ============================================================
    o = make_order(db, 8, "Bushra Anwar", "03334567890", "Lahore",
                   "House 19, Model Town, Lahore", 1950, 1, "postex", hours_ago=5 * 24)
    make_shipment(db, o, "postex", "PX10000777", pcs=1, cod=1950,
                  status="return_to_shipper",
                  booked_hours_ago=5 * 24, arrived_hours_ago=4 * 24)

    # ============================================================
    # GROUP G — 1 delivered + paid (happy completed sale, last week)
    # ============================================================
    o = make_order(db, 9, "Rabia Hussain", "03445566778", "Karachi",
                   "Bungalow 4, Defence Phase 6, Karachi", 5500, 3, "daewoo", hours_ago=8 * 24)
    sh = make_shipment(db, o, "daewoo", "DW55670000", pcs=3, cod=5500,
                       status="delivered",
                       booked_hours_ago=8 * 24, arrived_hours_ago=7 * 24,
                       delivered_hours_ago=6 * 24)
    make_payment(db, sh, amount=5500, hours_ago=2 * 24)

    db.commit()


def main():
    print("Initializing database...")
    init_db()
    db = SessionLocal()
    try:
        print("Clearing any previous test data...")
        wipe_test_data(db)
        print("Seeding 9 realistic test orders...")
        seed(db)
        # quick summary
        from app.db.models import Order as O, Shipment as S
        total_orders = db.query(O).count()
        total_shipments = db.query(S).count()
        print(f"\n[OK] Done. {total_orders} orders, {total_shipments} shipments in DB.\n")
        print("Now refresh your browser at http://localhost:8000 to see the dashboard light up.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
