"""Eigene Bestellungen (eBay Fulfillment API) – was wurde verkauft, was zusammen gekauft?"""
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, ebay_auth

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    date       TEXT NOT NULL,
    items      TEXT NOT NULL,       -- JSON: [{title, item_id, qty, price, shipping}]
    stored_at  TEXT NOT NULL
);
"""


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
            "order_id": o["orderId"],
            "date": o["creationDate"][:10],
            "items": [
                {"title": li.get("title", ""), "item_id": li.get("legacyItemId"),
                 "qty": li.get("quantity", 1), "price": float(li.get("lineItemCost", {}).get("value", 0)),
                 "shipping": float(li.get("deliveryCost", {}).get("shippingCost", {}).get("value", 0))}
                for li in o.get("lineItems", [])
            ],
        })
    return out



# ── Lokales Archiv: eBay liefert nur 90 Tage, hier wächst die Historie mit ──

def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)


def store_recent() -> int:
    """Neue Bestellungen von eBay ins lokale Archiv übernehmen (vorhandene bleiben erhalten)."""
    import json
    orders = recent_orders()
    now = db.now_iso()
    with db.connect() as con:
        for o in orders:
            con.execute("INSERT OR REPLACE INTO orders(order_id, date, items, stored_at) VALUES (?, ?, ?, ?)",
                        (o["order_id"], o["date"], json.dumps(o["items"], ensure_ascii=False), now))
    return len(orders)


def all_orders() -> list[dict]:
    """Alle archivierten Bestellungen – Grundlage für Analysen."""
    import json
    with db.connect() as con:
        rows = con.execute("SELECT * FROM orders ORDER BY date DESC").fetchall()
    return [{"order_id": r["order_id"], "date": r["date"], "items": json.loads(r["items"])} for r in rows]
