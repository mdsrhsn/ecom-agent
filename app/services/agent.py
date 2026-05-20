"""
Claude agent with tool-use.

You can chat with this from the dashboard:
  "aaj kitne orders aaye Karachi se?"
  "critical parcels kaunse hain?"
  "PostEx ki kitni payment pending hai?"
  "is hafte kitni inventory courier ko gayi?"

Claude calls tools to query the DB and returns a clean answer.
"""
import json
from datetime import datetime, timedelta
from anthropic import Anthropic
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.config import settings
from app.db import SessionLocal
from app.models import Order, Shipment, Payment, ShipmentStatus

client = Anthropic(api_key=settings.anthropic_api_key)

MODEL = "claude-sonnet-4-20250514"


# ---------- Tools the agent can call ----------

TOOLS = [
    {
        "name": "count_orders",
        "description": "Count Shopify orders within a date range, optionally filtered by city.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "description": "How many days back from today, e.g. 1=today, 7=last week"},
                "city": {"type": "string", "description": "Optional city filter"},
            },
            "required": ["days_back"],
        },
    },
    {
        "name": "count_shipments_by_status",
        "description": "Count shipments grouped by status (BOOKED, DELIVERED, RETURN_IN_PROCESS, etc.) optionally filtered by courier.",
        "input_schema": {
            "type": "object",
            "properties": {
                "courier": {"type": "string", "description": "Optional: postex, daewoo, digidokaan, leopards"},
            },
        },
    },
    {
        "name": "list_critical_shipments",
        "description": "List all currently critical (3+ days undelivered) shipments with customer details.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_payment_overdue",
        "description": "List shipments delivered 7+ days ago with no COD payment received.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "inventory_ledger",
        "description": "Get the full inventory ledger: pieces shipped, paid, returned, pending.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_order",
        "description": "Find an order by Shopify order number or tracking number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Order number (e.g. '#1042') or tracking number"},
            },
            "required": ["query"],
        },
    },
]


# ---------- Tool implementations ----------

def _tool_count_orders(db: Session, days_back: int, city: str | None = None):
    since = datetime.utcnow() - timedelta(days=days_back)
    q = db.query(Order).filter(Order.created_at >= since)
    if city:
        q = q.filter(Order.city.ilike(f"%{city}%"))
    rows = q.all()
    by_city = {}
    for o in rows:
        by_city[o.city or "Unknown"] = by_city.get(o.city or "Unknown", 0) + 1
    return {"total": len(rows), "by_city": by_city, "days_back": days_back}


def _tool_count_by_status(db: Session, courier: str | None = None):
    q = db.query(Shipment.status, func.count(Shipment.id))
    if courier:
        q = q.filter(Shipment.courier == courier.lower())
    return {status: count for status, count in q.group_by(Shipment.status).all()}


def _tool_list_critical(db: Session):
    items = db.query(Shipment).filter(
        Shipment.is_critical == True,
        Shipment.delivered_at.is_(None),
    ).all()
    return [
        {
            "cn": s.tracking_number,
            "courier": s.courier,
            "order": s.order.order_number if s.order else "",
            "customer": s.order.customer_name if s.order else "",
            "phone": s.order.customer_phone if s.order else "",
            "city": s.order.city if s.order else "",
            "cod": s.order.cod_amount if s.order else 0,
            "days": (datetime.utcnow() - s.booked_at).days if s.booked_at else 0,
        }
        for s in items
    ]


def _tool_payment_overdue(db: Session):
    cutoff = datetime.utcnow() - timedelta(days=settings.payment_overdue_days)
    items = db.query(Shipment).filter(
        Shipment.status == ShipmentStatus.DELIVERED.value,
        Shipment.delivered_at < cutoff,
    ).all()
    overdue = [s for s in items if not s.payments]
    return [
        {
            "cn": s.tracking_number,
            "courier": s.courier,
            "amount": s.order.cod_amount if s.order else 0,
            "days": (datetime.utcnow() - s.delivered_at).days,
        }
        for s in overdue
    ]


def _tool_inventory_ledger(db: Session):
    from app.services.reports import build_report
    return build_report(db)["inventory_ledger"]


def _tool_search_order(db: Session, query: str):
    q = query.strip().lstrip("#")
    # Try shipment by tracking number
    sh = db.query(Shipment).filter(Shipment.tracking_number == query.strip()).first()
    if sh:
        return _shipment_detail(sh)
    # Try order by number
    order = db.query(Order).filter(Order.order_number.ilike(f"%{q}%")).first()
    if order and order.shipments:
        return _shipment_detail(order.shipments[-1])
    return {"error": "Not found"}


def _shipment_detail(sh: Shipment) -> dict:
    return {
        "cn": sh.tracking_number,
        "courier": sh.courier,
        "status": sh.status,
        "is_critical": sh.is_critical,
        "order": sh.order.order_number if sh.order else "",
        "customer": sh.order.customer_name if sh.order else "",
        "phone": sh.order.customer_phone if sh.order else "",
        "city": sh.order.city if sh.order else "",
        "cod": sh.order.cod_amount if sh.order else 0,
        "booked_at": sh.booked_at.isoformat() if sh.booked_at else None,
        "delivered_at": sh.delivered_at.isoformat() if sh.delivered_at else None,
        "events": [
            {"status": e.status, "note": e.note, "at": e.occurred_at.isoformat()}
            for e in sh.events
        ],
    }


def _dispatch_tool(name: str, args: dict) -> dict:
    db = SessionLocal()
    try:
        if name == "count_orders":
            return _tool_count_orders(db, **args)
        if name == "count_shipments_by_status":
            return _tool_count_by_status(db, **args)
        if name == "list_critical_shipments":
            return _tool_list_critical(db)
        if name == "list_payment_overdue":
            return _tool_payment_overdue(db)
        if name == "inventory_ledger":
            return _tool_inventory_ledger(db)
        if name == "search_order":
            return _tool_search_order(db, **args)
        return {"error": f"Unknown tool: {name}"}
    finally:
        db.close()


SYSTEM_PROMPT = """You are Mudassar's ecommerce operations assistant.

You help him understand his Shopify + multi-courier order flow. The user
speaks Urdu/Roman Urdu mixed with English — respond in the same style they use.

You have tools to query the live database. Always use a tool if the question
needs current data — don't guess.

Be concise. Lead with the numbers. If there are critical items or overdue
payments, surface them clearly. Use PKR for amounts.

Two return states matter: RETURN_IN_PROCESS (decision pending) is
different from RETURN_TO_SHIPPER (confirmed coming back). Don't conflate them.
"""


async def chat_with_agent(user_message: str, history: list[dict] | None = None) -> str:
    messages = list(history or [])
    messages.append({"role": "user", "content": user_message})

    # Up to 8 tool-use cycles
    for _ in range(8):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            # Append the assistant's tool-use block back into the conversation
            messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = _dispatch_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Final answer
        text_parts = [b.text for b in resp.content if b.type == "text"]
        return "\n".join(text_parts)

    return "(Agent stuck in tool loop — please rephrase.)"
