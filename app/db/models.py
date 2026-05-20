"""
Database models — the inventory + courier tracking ledger.

Order -> Shipment -> StatusEvents (timeline)
Shipment -> Payment

Status values:
  booked / arrived_warehouse / in_transit / delivered /
  return_in_process / return_to_shipper / received_back / cancelled / lost

return_in_process: parcel under return decision, MAY still deliver
return_to_shipper: confirmed, coming back to us
received_back: we physically have it again
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Float, Boolean, ForeignKey, Text, Index
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    shopify_order_id = Column(String, unique=True, index=True, nullable=False)
    order_number = Column(String, index=True)

    customer_name = Column(String)
    customer_phone = Column(String)
    customer_address = Column(Text)
    city = Column(String, index=True)
    province = Column(String)

    total_amount = Column(Float, default=0)
    cod_amount = Column(Float, default=0)
    items_count = Column(Integer, default=0)

    shopify_tags = Column(String)
    courier_hint = Column(String, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    shipments = relationship("Shipment", back_populates="order", cascade="all, delete-orphan")


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)

    courier = Column(String, index=True, nullable=False)
    tracking_number = Column(String, unique=True, index=True, nullable=False)

    pcs_count = Column(Integer, default=1)
    cod_amount = Column(Float, default=0)

    current_status = Column(String, default="booked", index=True)
    last_status_at = Column(DateTime, default=datetime.utcnow)

    booked_at = Column(DateTime, default=datetime.utcnow, index=True)
    arrived_warehouse_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True, index=True)
    returned_at = Column(DateTime, nullable=True)

    is_critical = Column(Boolean, default=False, index=True)
    is_payment_overdue = Column(Boolean, default=False, index=True)

    order = relationship("Order", back_populates="shipments")
    events = relationship("StatusEvent", back_populates="shipment", cascade="all, delete-orphan")
    payment = relationship("Payment", back_populates="shipment", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_shipment_status_courier", "current_status", "courier"),
    )


class StatusEvent(Base):
    __tablename__ = "status_events"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, index=True)

    status = Column(String, nullable=False)
    raw_status = Column(String)
    description = Column(Text)
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    shipment = relationship("Shipment", back_populates="events")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, unique=True)

    amount_received = Column(Float, default=0)
    expected_amount = Column(Float, default=0)
    courier_fee = Column(Float, default=0)
    received_at = Column(DateTime, default=datetime.utcnow, index=True)

    has_mismatch = Column(Boolean, default=False)
    note = Column(Text)

    shipment = relationship("Shipment", back_populates="payment")


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), index=True, nullable=True)
    alert_type = Column(String, nullable=False)
    channel = Column(String)
    recipient = Column(String)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    message_preview = Column(Text)


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, unique=True)
    email = Column(String)
    role = Column(String, default="agent")
    receives_alerts = Column(Boolean, default=True)
