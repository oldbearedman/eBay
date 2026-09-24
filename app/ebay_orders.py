"""Eigene Bestellungen (eBay Fulfillment API) – was wurde verkauft, was zusammen gekauft?"""
from datetime import datetime, timedelta, timezone

import httpx

from . import config, ebay_auth


def _fetch(since: datetime) -> list[dict]:
    orders, offset = [], 0
    stamp = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    while True:
        r = httpx.get(
            f"{config.API_BASE}/sell/fulfillment/v1/order",
            params={"filter": f"creationdate:[{stamp}..]", "limit": 200, "offset": offset},
            headers={"Authorization": f"Bearer {ebay_auth.access_token()}"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        orders += data.get("orders", [])
        offset += 200
        if offset >= data.get("total", 0):
            return orders


def recent_orders(days: int = 730) -> list[dict]:
    """Bestellungen der letzten `days` Tage (fällt auf 90 Tage zurück, falls eBay mehr ablehnt)."""
    now = datetime.now(timezone.utc)
    try:
        raw = _fetch(now - timedelta(days=days))
    except httpx.HTTPStatusError:
        raw = _fetch(now - timedelta(days=90))
    out = []
    for o in raw:
        if o.get("orderPaymentStatus") in ("FAILED",) or o.get("cancelStatus", {}).get("cancelState") == "CANCELED":
            continue
        out.append({
            "date": o["creationDate"][:10],
            "items": [
                {"title": li.get("title", ""), "item_id": li.get("legacyItemId"),
                 "qty": li.get("quantity", 1), "price": float(li.get("lineItemCost", {}).get("value", 0)),
                 "shipping": float(li.get("deliveryCost", {}).get("shippingCost", {}).get("value", 0))}
                for li in o.get("lineItems", [])
            ],
        })
    return out
