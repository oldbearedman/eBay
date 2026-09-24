"""Gleicht die lokale Datenbank mit den aktiven eBay-Angeboten ab."""
from . import db, ebay_trading


def run_sync() -> int:
    try:
        items = ebay_trading.get_active_listings()
    except Exception as exc:  # Fehler protokollieren, Oberfläche zeigt ihn an
        with db.connect() as con:
            con.execute("INSERT INTO sync_log(at, ok, message) VALUES (?, 0, ?)", (db.now_iso(), str(exc)[:500]))
        raise
    now = db.now_iso()
    with db.connect() as con:
        con.execute("UPDATE listings SET active = 0")
        for it in items:
            con.execute(
                """INSERT INTO listings(item_id, title, price, currency, quantity, start_time,
                       watch_count, url, image_url, listing_type, sku, active, synced_at)
                   VALUES(:item_id, :title, :price, :currency, :quantity, :start_time,
                       :watch_count, :url, :image_url, :listing_type, :sku, 1, :synced_at)
                   ON CONFLICT(item_id) DO UPDATE SET
                       title=excluded.title, price=excluded.price, currency=excluded.currency,
                       quantity=excluded.quantity, start_time=excluded.start_time,
                       watch_count=excluded.watch_count, url=excluded.url,
                       image_url=excluded.image_url, listing_type=excluded.listing_type,
                       sku=excluded.sku, active=1, synced_at=excluded.synced_at""",
                {**it, "synced_at": now},
            )
        con.execute("INSERT INTO sync_log(at, ok, count) VALUES (?, 1, ?)", (now, len(items)))
    return len(items)
