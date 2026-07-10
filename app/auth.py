"""
Lightweight access control.

The dashboard and agent-chat endpoints expose business data, so they must not be
open to anyone with the URL. We gate them behind a shared secret (APP_SECRET).

Two ways to pass the key:
  1. Query string:  /?key=YOUR_SECRET           (browser-friendly, like the admin panel)
  2. Header:        X-API-Key: YOUR_SECRET       (for programmatic / fetch calls)

Shopify webhooks are NOT gated here — they authenticate via HMAC (SHOPIFY_WEBHOOK_SECRET).

If APP_SECRET is left at its default ("change-me"), auth is disabled so local dev
still works. In production, set a real APP_SECRET and this turns on automatically.
"""
from fastapi import HTTPException, Query, Header, status
from app.config import settings


def _auth_enabled() -> bool:
    return bool(settings.APP_SECRET) and settings.APP_SECRET != "change-me"


def require_key(
    key: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
) -> bool:
    """FastAPI dependency. Raises 401 unless a valid key is supplied."""
    if not _auth_enabled():
        return True  # dev mode — no secret configured

    supplied = key or x_api_key or ""
    if supplied != settings.APP_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid access key. Add ?key=YOUR_APP_SECRET to the URL.",
        )
    return True
