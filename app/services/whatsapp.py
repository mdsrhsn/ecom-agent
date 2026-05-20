"""WhatsApp Cloud API sender."""
import httpx
from app.config import settings


async def send_message(to_phone: str, body: str) -> dict:
    if not (settings.WHATSAPP_PHONE_ID and settings.WHATSAPP_ACCESS_TOKEN):
        return {"error": "WhatsApp not configured"}

    url = f"https://graph.facebook.com/v21.0/{settings.WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body[:4096]},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            return {"error": str(e)}


async def broadcast(body: str, phones=None) -> list:
    targets = phones if phones is not None else settings.all_notify_phones
    results = []
    for phone in targets:
        results.append({"phone": phone, "result": await send_message(phone, body)})
    return results
