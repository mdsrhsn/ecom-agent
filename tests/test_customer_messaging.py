"""Unit tests for customer WhatsApp status + feedback agent."""
import asyncio
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

# Ensure repo root on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_customer_messaging.db")
os.environ.setdefault("CUSTOMER_WHATSAPP_ENABLED", "true")
os.environ.setdefault("STORE_NAME", "Women Comforts")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Order,
    Shipment,
    CustomerConversation,
    CustomerMessageLog,
)
from app.services.customer_messaging import (
    normalize_phone,
    build_status_message,
    notify_customer_status_change,
    handle_inbound_customer_message,
)
from app.services import postex, daewoo, digidokaan


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class PhoneNormalizeTests(unittest.TestCase):
    def test_local_0_prefix(self):
        self.assertEqual(normalize_phone("03001234567"), "923001234567")

    def test_already_international(self):
        self.assertEqual(normalize_phone("+92 300 1234567"), "923001234567")

    def test_10_digit_mobile(self):
        self.assertEqual(normalize_phone("3001234567"), "923001234567")

    def test_empty(self):
        self.assertIsNone(normalize_phone(""))
        self.assertIsNone(normalize_phone(None))


class StatusMapTests(unittest.TestCase):
    def test_postex_out_for_delivery(self):
        self.assertEqual(postex.normalize_status("Out for Delivery"), "out_for_delivery")
        self.assertEqual(postex.normalize_status("Dispatched"), "in_transit")
        self.assertEqual(postex.normalize_status("Delivered"), "delivered")

    def test_daewoo_out_for_delivery(self):
        self.assertEqual(daewoo.normalize_status("Out for Delivery"), "out_for_delivery")

    def test_digidokaan_out_for_delivery(self):
        self.assertEqual(digidokaan.normalize_status("Out For Delivery"), "out_for_delivery")


class TemplateTests(unittest.TestCase):
    def test_delivered_asks_feedback(self):
        order = Order(
            shopify_order_id="1",
            order_number="#1001",
            customer_name="Ayesha Khan",
            city="Lahore",
        )
        sh = Shipment(
            order_id=1,
            courier="postex",
            tracking_number="PX123",
            current_status="delivered",
        )
        msg = build_status_message("delivered", order, sh)
        self.assertIn("feedback", msg.lower())
        self.assertIn("Product kaisa", msg)
        self.assertIn("Quality", msg)

    def test_out_for_delivery_mentions_rider(self):
        order = Order(
            shopify_order_id="1",
            order_number="#1001",
            customer_name="Ali",
        )
        sh = Shipment(
            order_id=1,
            courier="postex",
            tracking_number="PX999",
            current_status="out_for_delivery",
        )
        msg = build_status_message("out_for_delivery", order, sh)
        self.assertIn("delivery", msg.lower())
        self.assertIn("PX999", msg)


class MessagingFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        self.order = Order(
            shopify_order_id="TEST-WA-1",
            order_number="#2001",
            customer_name="Sara Ahmed",
            customer_phone="03001112233",
            city="Karachi",
            total_amount=2500,
            cod_amount=2500,
            items_count=1,
        )
        self.db.add(self.order)
        self.db.flush()
        self.shipment = Shipment(
            order_id=self.order.id,
            courier="postex",
            tracking_number="PX-WA-001",
            pcs_count=1,
            cod_amount=2500,
            current_status="in_transit",
            booked_at=datetime.utcnow(),
        )
        self.db.add(self.shipment)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch(
        "app.services.customer_messaging.whatsapp.send_message",
        new_callable=AsyncMock,
        return_value={"messages": [{"id": "wamid.TEST1"}]},
    )
    def test_status_change_sends_once(self, mock_send):
        result = run(
            notify_customer_status_change(
                self.db, self.shipment, "out_for_delivery", old_status="in_transit"
            )
        )
        self.db.commit()
        self.assertTrue(result.get("sent"))
        self.assertEqual(mock_send.await_count, 1)
        self.assertEqual(mock_send.await_args.args[0], "923001112233")

        # Second call must dedupe
        result2 = run(
            notify_customer_status_change(
                self.db, self.shipment, "out_for_delivery", old_status="in_transit"
            )
        )
        self.assertTrue(result2.get("skipped"))
        self.assertEqual(mock_send.await_count, 1)

        logs = self.db.query(CustomerMessageLog).all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message_type, "status_out_for_delivery")

    @patch(
        "app.services.customer_messaging.whatsapp.broadcast",
        new_callable=AsyncMock,
        return_value=[],
    )
    @patch(
        "app.services.customer_messaging.whatsapp.send_message",
        new_callable=AsyncMock,
        return_value={"messages": [{"id": "wamid.TEST2"}]},
    )
    def test_delivered_feedback_then_followup(self, mock_send, mock_broadcast):
        result = run(
            notify_customer_status_change(
                self.db, self.shipment, "delivered", old_status="out_for_delivery"
            )
        )
        self.db.commit()
        self.assertTrue(result.get("sent"))

        conv = (
            self.db.query(CustomerConversation)
            .filter(CustomerConversation.shipment_id == self.shipment.id)
            .first()
        )
        self.assertIsNotNone(conv)
        self.assertEqual(conv.state, "awaiting_feedback")

        inbound = run(
            handle_inbound_customer_message(
                self.db,
                "923001112233",
                "Product bohot acha laga, quality theek hai, sab mila.",
                wa_message_id="wamid.in.1",
            )
        )
        self.db.commit()
        self.assertTrue(inbound.get("ok"))
        self.assertEqual(inbound.get("action"), "feedback_saved")

        self.db.refresh(conv)
        self.assertEqual(conv.state, "followup_sent")
        self.assertIn("acha", conv.feedback_text)

        # One delivery msg + one follow-up
        outbound_types = {
            m.message_type
            for m in self.db.query(CustomerMessageLog)
            .filter(CustomerMessageLog.direction == "outbound")
            .all()
        }
        self.assertIn("status_delivered", outbound_types)
        self.assertIn("followup", outbound_types)
        self.assertGreaterEqual(mock_send.await_count, 2)
        mock_broadcast.assert_awaited()  # team notified of feedback


if __name__ == "__main__":
    unittest.main()
