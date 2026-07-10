# Ecom Agent — Order Lifecycle Manager

AI agent for Shopify + multi-courier ecommerce. Tracks orders from creation to delivery to payment, flags critical shipments, and answers Roman Urdu / English queries via a chat dashboard.

## What it does

1. **New order summary** — daily city-wise count (Lahore: 8, Islamabad: 6, etc.)
2. **Courier booking tracker** — auto-detects courier from Shopify tags + tracking-number prefix (PX→PostEx, DW→Daewoo, DD→DigiDokaan)
3. **Warehouse arrival detection** — courier API polling every 3 hours
4. **CRITICAL alert** — any parcel undelivered for 3+ days flagged for team follow-up calls
5. **Payment overdue alert** — 7+ days after delivery, no COD payment received
6. **Inventory ledger** — live tally of pieces sent / paid / returned / pending
7. **Return state tracking** — `return_in_process` (under decision) vs `return_to_shipper` (confirmed coming back), tracked separately
8. **Notifications** — owner + team via WhatsApp + email + web dashboard
9. **Chat agent** — Google Gemini powered, answers natural Roman Urdu queries

## Tech stack

- FastAPI (webhooks + dashboard)
- Google Gemini (gemini-2.5-flash-lite) agentic loop with 9 custom tools
- SQLAlchemy + Postgres (Railway addon) / SQLite for local dev
- APScheduler (3-hourly tracking poll + daily 9 AM PKT summary)
- Jinja2 dashboard with auto-refresh chat panel

## Local setup

```bash
git clone <repo>
cd ecom-agent
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in API keys
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000` for dashboard.

## Railway deployment

1. Push to GitHub
2. Create new Railway project from repo
3. Add Postgres addon → auto-injects `DATABASE_URL`
4. Set env vars from `.env.example`
5. Deploy — Procfile handles startup

## Access control

The dashboard and `/agent/chat` expose live business data, so they are gated behind
a shared secret (`APP_SECRET`). In production, set a real `APP_SECRET` and open the
dashboard as:

```
https://your-app.railway.app/?key=YOUR_APP_SECRET
```

Programmatic calls can instead send an `X-API-Key: YOUR_APP_SECRET` header. If
`APP_SECRET` is left as `change-me`, the check is disabled (handy for local dev).
Shopify webhooks are not gated by this key — they authenticate via HMAC using
`SHOPIFY_WEBHOOK_SECRET`.

## Shopify webhook config

In Shopify admin → Settings → Notifications → Webhooks, add:

| Event | URL |
|---|---|
| `orders/create` | `https://your-app.railway.app/webhooks/shopify/orders-create` |
| `fulfillments/create` (Shopify event) | `https://your-app.railway.app/webhooks/shopify/fulfillment-create` |

Set webhook secret = `SHOPIFY_WEBHOOK_SECRET` env var.

## Courier detection logic

The agent identifies courier in this priority order:
1. Shopify order tag (`postex`, `daewoo`, `digidokaan`, `leopards`, `tcs`)
2. Tracking number prefix:
   - `PX` → PostEx
   - `DW` → Daewoo
   - `DD` → DigiDokaan
   - `LP` → Leopards
   - `TCS` → TCS

## Status normalization

All couriers map to a single status vocabulary:
- `booked` — order booked, awaiting pickup
- `arrived_warehouse` — courier picked up parcel
- `in_transit` — out for delivery
- `delivered` — delivered to customer
- `return_in_process` — pending return decision
- `return_to_shipper` — confirmed coming back
- `received_back` — back in your warehouse
- `cancelled` / `lost`

## Inventory formula

```
pcs_pending = pcs_sent − pcs_paid − pcs_return_to_shipper − pcs_received_back
```

## Agent chat examples

- *"Aaj kitne orders aye?"* → city-wise breakdown
- *"Critical parcels dikhao"* → 3+ day undelivered list
- *"PostEx ki inventory kya hai?"* → courier-wise ledger
- *"PX12345 ka status?"* → single shipment trace
- *"Payment overdue kitne hain?"* → 7+ day post-delivery unpaid list

## Project structure

```
app/
├── main.py                 # FastAPI entry + APScheduler lifespan
├── config.py               # pydantic-settings
├── db/
│   ├── models.py           # Order, Shipment, StatusEvent, Payment, AlertLog
│   └── session.py
├── services/
│   ├── shopify.py          # HMAC verify, courier detection
│   ├── postex.py           # PostEx API + status map
│   ├── daewoo.py           # Daewoo API + status map
│   ├── digidokaan.py       # DigiDokaan API + status map
│   ├── couriers.py         # unified dispatcher
│   ├── whatsapp.py         # Meta Cloud API v21.0
│   └── email.py            # SMTP
├── agent/
│   ├── tools.py            # 9 tool specs + implementations
│   └── claude_client.py    # agentic loop, max 6 rounds
├── jobs.py                 # poll_active_shipments, daily_summary
└── routes/
    ├── api.py              # webhooks + /agent/chat
    └── dashboard.py        # HTML + summary JSON

templates/
└── dashboard.html          # live dashboard with chat panel
```
