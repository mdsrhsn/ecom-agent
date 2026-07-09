"""
Tools the Claude agent can call.

These map to the questions Mudassar can ask:
  - "aaj kitne orders aaye?"      -> get_daily_order_summary
  - "kis courier ko kitne booked?" -> get_courier_booking_summary
  - "warehouse pohanche?"          -> get_warehouse_arrivals
  - "critical parcels?"            -> get_critical_shipments
  - "payment overdue?"             -> get_payment_overdue
  - "inventory batao?"             -> get_inventory_ledger
  - "returns?"                     -> get_returns_breakdown
"""
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.db.models import Order, Shipment, Payment, StatusEvent
from app.services import couriers


TOOL_SPECS = [
    {
        "name": "get_daily_order_summary",
        "description": (
            "Returns count of new orders received today, broken down by city. "
            "Use when user asks 'aaj kitne orders aaye' or 'order summary'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {
                    "type": "integer",
                    "description": "0 = today, 1 = yesterday, 7 = last week",
                    "default": 0,
                }
            },
        },
    },
    {
        "name": "get_courier_booking_summary",
        "description": (
            "Returns how many shipments were booked today per courier "
            "(postex, daewoo, digidokaan). Use for 'kis courier ko kitne'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days_back": {"type": "integer", "default": 0}},
        },
    },
    {
        "name": "get_warehouse_arrivals",
        "description": (
            "Returns shipments that arrived at courier warehouse today (picked up). "
            "Use for 'kitne parcels courier ke warehouse pohanche'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days_back": {"type": "integer", "default": 0}},
        },
    },
    {
        "name": "get_critical_shipments",
        "description": (
            "Returns parcels booked 3+ days ago but not yet delivered. "
            "These need urgent follow-up. Use for 'critical' or 'follow-up'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"min_days": {"type": "integer", "default": 3}},
        },
    },
    {
        "name": "get_payment_overdue",
        "description": (
            "Returns delivered parcels with no payment in 7+ days. "
            "Use for 'payment pending' or 'paise kab milenge'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"min_days": {"type": "integer", "default": 7}},
        },
    },
    {
        "name": "get_inventory_ledger",
        "description": (
            "Live inventory tally: pcs sent to courier, pcs paid, pcs returned "
            "(in process / to shipper / received back), pcs pending. "
            "Optional courier filter. Use for 'inventory batao'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "courier": {
                    "type": "string",
                    "description": "Optional: postex / daewoo / digidokaan",
                }
            },
        },
    },
    {
        "name": "get_returns_breakdown",
        "description": (
            "Returns split by state: return_in_process (still under decision), "
            "return_to_shipper (confirmed coming back), received_back (we have it). "
            "Use for 'returns batao'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_shipment_tracking",
        "description": "Fetch latest status from courier API for a specific tracking number.",
        "input_schema": {
            "type": "object",
            "properties": {"tracking_number": {"type": "string"}},
            "required": ["tracking_number"],
        },
    },
    {
        "name": "search_shipment",
        "description": "Find a shipment by tracking number or order number.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


def _date_range(days_back: int):
    """
    Return (start, end) UTC datetimes for "today" in Pakistan time (Asia/Karachi, UTC+5).
    Pakistan has no DST so a fixed +5 hour offset is correct year-round.
    Orders are stored as UTC in DB; we shift the day boundary so that
    "today" matches what Mudassar sees on his clock in Pakistan.
    """
    PKT_OFFSET = timedelta(hours=5)
    now_pkt = datetime.utcnow() + PKT_OFFSET
    today_pkt_midnight = now_pkt.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert that PKT midnight back to UTC for the DB filter:
    start_utc = today_pkt_midnight - PKT_OFFSET - timedelta(days=days_back)
    end_utc = start_utc + timedelta(days=1)
    return start_utc, end_utc


def pkt_range(period: str = "today", from_date: str = None, to_date: str = None):
    """
    Return (start_utc, end_utc) for a Pakistan-time date window.

    period:
      - "today"     -> just today in PKT
      - "last_7"    -> last 7 PKT days (including today)
      - "last_30"   -> last 30 PKT days (including today)
      - "custom"    -> use from_date / to_date (YYYY-MM-DD strings, inclusive)
    """
    PKT_OFFSET = timedelta(hours=5)
    now_pkt = datetime.utcnow() + PKT_OFFSET
    today_pkt = now_pkt.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "custom" and from_date and to_date:
        try:
            f = datetime.strptime(from_date, "%Y-%m-%d")
            t = datetime.strptime(to_date, "%Y-%m-%d")
            start_pkt = f.replace(hour=0, minute=0, second=0, microsecond=0)
            end_pkt = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            # Fall back to today on bad input
            start_pkt = today_pkt
            end_pkt = today_pkt + timedelta(days=1)
    elif period == "last_7":
        start_pkt = today_pkt - timedelta(days=6)  # 7-day window including today
        end_pkt = today_pkt + timedelta(days=1)
    elif period == "last_30":
        start_pkt = today_pkt - timedelta(days=29)  # 30-day window including today
        end_pkt = today_pkt + timedelta(days=1)
    else:  # today
        start_pkt = today_pkt
        end_pkt = today_pkt + timedelta(days=1)

    # Convert PKT boundaries back to UTC for DB filter
    return start_pkt - PKT_OFFSET, end_pkt - PKT_OFFSET


def get_orders_in_range(db: Session, start, end, label: str = "today") -> dict:
    """Same as get_daily_order_summary but accepts explicit UTC (start, end)."""
    total = (
        db.query(func.count(Order.id))
        .filter(Order.created_at >= start, Order.created_at < end)
        .scalar() or 0
    )
    rows = (
        db.query(Order.city, func.count(Order.id))
        .filter(Order.created_at >= start, Order.created_at < end)
        .group_by(Order.city)
        .order_by(func.count(Order.id).desc())
        .all()
    )
    by_city = {(city or "Unknown"): cnt for city, cnt in rows}
    return {
        "date": label,
        "total_orders": total,
        "by_city": by_city,
    }


def get_bookings_in_range(db: Session, start, end, label: str = "today") -> dict:
    """Same as get_courier_booking_summary but accepts explicit UTC (start, end)."""
    rows = (
        db.query(Shipment.courier, func.count(Shipment.id), func.sum(Shipment.pcs_count))
        .filter(Shipment.booked_at >= start, Shipment.booked_at < end)
        .group_by(Shipment.courier)
        .all()
    )
    by_courier = {
        courier: {"shipments": cnt, "pcs": int(pcs or 0)}
        for courier, cnt, pcs in rows
    }
    total = sum(v["shipments"] for v in by_courier.values())
    return {
        "date": label,
        "total_booked": total,
        "by_courier": by_courier,
    }


def get_daily_order_summary(db: Session, days_back: int = 0) -> dict:
    start, end = _date_range(days_back)
    total = (
        db.query(func.count(Order.id))
        .filter(Order.created_at >= start, Order.created_at < end)
        .scalar() or 0
    )
    rows = (
        db.query(Order.city, func.count(Order.id))
        .filter(Order.created_at >= start, Order.created_at < end)
        .group_by(Order.city)
        .order_by(func.count(Order.id).desc())
        .all()
    )
    by_city = {(city or "Unknown"): cnt for city, cnt in rows}
    return {
        "date": start.strftime("%Y-%m-%d"),
        "total_orders": total,
        "by_city": by_city,
    }


def get_courier_booking_summary(db: Session, days_back: int = 0) -> dict:
    start, end = _date_range(days_back)
    rows = (
        db.query(Shipment.courier, func.count(Shipment.id), func.sum(Shipment.pcs_count))
        .filter(Shipment.booked_at >= start, Shipment.booked_at < end)
        .group_by(Shipment.courier)
        .all()
    )
    by_courier = {
        courier: {"shipments": cnt, "pcs": int(pcs or 0)}
        for courier, cnt, pcs in rows
    }
    total = sum(v["shipments"] for v in by_courier.values())
    return {
        "date": start.strftime("%Y-%m-%d"),
        "total_booked": total,
        "by_courier": by_courier,
    }


def get_warehouse_arrivals(db: Session, days_back: int = 0) -> dict:
    start, end = _date_range(days_back)
    rows = (
        db.query(Shipment.courier, func.count(Shipment.id))
        .filter(
            Shipment.arrived_warehouse_at >= start,
            Shipment.arrived_warehouse_at < end,
        )
        .group_by(Shipment.courier)
        .all()
    )
    return {
        "date": start.strftime("%Y-%m-%d"),
        "total_arrivals": sum(c for _, c in rows),
        "by_courier": {courier: cnt for courier, cnt in rows},
    }


def get_critical_shipments(db: Session, min_days: int = 3) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=min_days)
    active = [
        "booked", "arrived_warehouse", "in_transit",
        "out_for_delivery", "return_in_process",
    ]
    rows = (
        db.query(Shipment, Order)
        .join(Order, Shipment.order_id == Order.id)
        .filter(
            Shipment.booked_at <= cutoff,
            Shipment.current_status.in_(active),
            Shipment.delivered_at.is_(None),
        )
        .order_by(Shipment.booked_at.asc())
        .limit(100)
        .all()
    )
    items = []
    for sh, order in rows:
        days_old = (datetime.utcnow() - sh.booked_at).days
        items.append({
            "tracking": sh.tracking_number,
            "order_number": order.order_number,
            "courier": sh.courier,
            "customer": order.customer_name,
            "phone": order.customer_phone,
            "city": order.city,
            "status": sh.current_status,
            "days_since_booking": days_old,
            "cod_amount": sh.cod_amount,
        })
    return {"count": len(items), "items": items}


def get_payment_overdue(db: Session, min_days: int = 7) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=min_days)
    rows = (
        db.query(Shipment, Order)
        .join(Order, Shipment.order_id == Order.id)
        .outerjoin(Payment, Payment.shipment_id == Shipment.id)
        .filter(
            Shipment.current_status == "delivered",
            Shipment.delivered_at <= cutoff,
            Payment.id.is_(None),
        )
        .order_by(Shipment.delivered_at.asc())
        .limit(100)
        .all()
    )
    items = []
    total_pending = 0.0
    for sh, order in rows:
        days_old = (datetime.utcnow() - sh.delivered_at).days if sh.delivered_at else None
        items.append({
            "tracking": sh.tracking_number,
            "order_number": order.order_number,
            "courier": sh.courier,
            "days_since_delivery": days_old,
            "amount_due": sh.cod_amount,
        })
        total_pending += sh.cod_amount or 0
    return {
        "count": len(items),
        "total_pending_pkr": total_pending,
        "items": items,
    }


def get_inventory_ledger(db: Session, courier: str = None) -> dict:
    """
    pcs_pending = pcs_sent_to_courier - pcs_paid - pcs_return_to_shipper - pcs_received_back
    """
    base = db.query(Shipment)
    if courier:
        base = base.filter(Shipment.courier == courier)

    def sum_pcs(query):
        return query.with_entities(func.coalesce(func.sum(Shipment.pcs_count), 0)).scalar() or 0

    sent = sum_pcs(base.filter(Shipment.current_status != "booked"))
    rts = sum_pcs(base.filter(Shipment.current_status == "return_to_shipper"))
    received_back = sum_pcs(base.filter(Shipment.current_status == "received_back"))
    in_process = sum_pcs(base.filter(Shipment.current_status == "return_in_process"))

    paid_q = (
        db.query(func.coalesce(func.sum(Shipment.pcs_count), 0))
        .join(Payment, Payment.shipment_id == Shipment.id)
    )
    if courier:
        paid_q = paid_q.filter(Shipment.courier == courier)
    paid = paid_q.scalar() or 0

    pending = sent - paid - rts - received_back

    return {
        "courier_filter": courier or "all",
        "pcs_sent_to_courier": int(sent),
        "pcs_paid": int(paid),
        "pcs_return_in_process": int(in_process),
        "pcs_return_to_shipper": int(rts),
        "pcs_received_back": int(received_back),
        "pcs_pending": int(pending),
    }


def get_returns_breakdown(db: Session) -> dict:
    rows = (
        db.query(Shipment.current_status, func.count(Shipment.id), func.sum(Shipment.pcs_count))
        .filter(Shipment.current_status.in_(
            ["return_in_process", "return_to_shipper", "received_back"]
        ))
        .group_by(Shipment.current_status)
        .all()
    )
    breakdown = {
        status: {"shipments": cnt, "pcs": int(pcs or 0)}
        for status, cnt, pcs in rows
    }
    return {
        "return_in_process": breakdown.get("return_in_process", {"shipments": 0, "pcs": 0}),
        "return_to_shipper": breakdown.get("return_to_shipper", {"shipments": 0, "pcs": 0}),
        "received_back": breakdown.get("received_back", {"shipments": 0, "pcs": 0}),
    }


async def refresh_shipment_tracking(db: Session, tracking_number: str) -> dict:
    from app.services.customer_messaging import notify_customer_status_change

    sh = db.query(Shipment).filter(Shipment.tracking_number == tracking_number).first()
    if not sh:
        return {"error": f"Shipment {tracking_number} not found"}

    result = await couriers.track_shipment(sh.courier, tracking_number)
    if "error" in result:
        return result

    new_status = result.get("normalized_status", "unknown")
    if new_status != sh.current_status and new_status != "unknown":
        old_status = sh.current_status
        sh.current_status = new_status
        sh.last_status_at = datetime.utcnow()

        if new_status == "arrived_warehouse" and not sh.arrived_warehouse_at:
            sh.arrived_warehouse_at = datetime.utcnow()
        if new_status == "delivered" and not sh.delivered_at:
            sh.delivered_at = datetime.utcnow()
        if new_status in ("return_to_shipper", "received_back") and not sh.returned_at:
            sh.returned_at = datetime.utcnow()

        db.add(StatusEvent(
            shipment_id=sh.id,
            status=new_status,
            raw_status=result.get("raw_status", ""),
            description=result.get("description", ""),
        ))
        db.flush()

        customer_notify = await notify_customer_status_change(
            db, sh, new_status, old_status=old_status
        )
        db.commit()
        return {
            "tracking": tracking_number,
            "courier": sh.courier,
            "old_status": old_status,
            "new_status": new_status,
            "raw_status": result.get("raw_status", ""),
            "description": result.get("description", ""),
            "customer_notify": customer_notify,
        }

    return {
        "tracking": tracking_number,
        "courier": sh.courier,
        "old_status": sh.current_status,
        "new_status": new_status,
        "raw_status": result.get("raw_status", ""),
        "description": result.get("description", ""),
        "note": "no change",
    }


def search_shipment(db: Session, query: str) -> dict:
    q = query.strip().lstrip("#")
    hit = (
        db.query(Shipment, Order)
        .join(Order, Shipment.order_id == Order.id)
        .filter(
            (Shipment.tracking_number == q)
            | (Order.order_number == f"#{q}")
            | (Order.order_number == q)
        )
        .first()
    )
    if not hit:
        return {"found": False, "query": query}
    s, o = hit
    return {
        "found": True,
        "tracking": s.tracking_number,
        "order_number": o.order_number,
        "courier": s.courier,
        "status": s.current_status,
        "customer": o.customer_name,
        "phone": o.customer_phone,
        "city": o.city,
        "booked_at": s.booked_at.isoformat() if s.booked_at else None,
        "delivered_at": s.delivered_at.isoformat() if s.delivered_at else None,
        "cod_amount": s.cod_amount,
    }


TOOL_IMPL = {
    "get_daily_order_summary": (get_daily_order_summary, False),
    "get_courier_booking_summary": (get_courier_booking_summary, False),
    "get_warehouse_arrivals": (get_warehouse_arrivals, False),
    "get_critical_shipments": (get_critical_shipments, False),
    "get_payment_overdue": (get_payment_overdue, False),
    "get_inventory_ledger": (get_inventory_ledger, False),
    "get_returns_breakdown": (get_returns_breakdown, False),
    "refresh_shipment_tracking": (refresh_shipment_tracking, True),
    "search_shipment": (search_shipment, False),
}


async def run_tool(name: str, args: dict, db: Session) -> dict:
    if name not in TOOL_IMPL:
        return {"error": f"Unknown tool: {name}"}
    fn, is_async = TOOL_IMPL[name]
    try:
        if is_async:
            return await fn(db, **args)
        return fn(db, **args)
    except TypeError as e:
        return {"error": f"Bad args for {name}: {e}"}
    except Exception as e:
        return {"error": f"Tool {name} failed: {e}"}
