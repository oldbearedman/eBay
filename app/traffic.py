"""Impressionen & Aufrufe je Angebot (eBay Analytics API) und Diagnose: Preis- oder Nachfrageblocker?"""
import statistics
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config, db, ebay_auth

DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic (
    item_id      TEXT PRIMARY KEY,
    impressions  INTEGER NOT NULL,
    views        INTEGER NOT NULL,
    transactions INTEGER NOT NULL,
    period_days  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

DIAGNOSES = {
    "zu_teuer":   ("💸", "Zu teuer", "Deutlich über Markt – Preis senken oder bündeln."),
    "unsichtbar": ("👻", "Kaum gesehen", "Preis passt, aber kaum jemand sucht danach – als Beipack in ein Bündel."),
    "klick":      ("🙈", "Gesehen, nicht geklickt", "Erscheint oft in der Suche, wird aber selten angeklickt – Titel, erstes Bild oder Preis verbessern."),
    "kauf":       ("🤔", "Viele Aufrufe, kein Kauf", "Interesse ist da – Beschreibung/Zustand/Versand prüfen oder Beobachtern ein Angebot machen."),
    "ok":         ("✅", "Läuft normal", "Keine Auffälligkeit."),
    "neu":        ("🆕", "Zu neu", "Weniger als 7 Tage online – noch nicht aussagekräftig."),
}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)


def refresh() -> int:
    """Holt die Zahlen der letzten 30 Tage für alle aktiven Angebote (je 200 pro Abruf)."""
    with db.connect() as con:
        ids = [r["item_id"] for r in con.execute("SELECT item_id FROM listings WHERE active = 1")]
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=DAYS - 1)
    now = db.now_iso()
    count = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        r = httpx.get(
            f"{config.API_BASE}/sell/analytics/v1/traffic_report",
            params={
                "dimension": "LISTING",
                "filter": (f"marketplace_ids:{{EBAY_DE}},date_range:[{start:%Y%m%d}..{end:%Y%m%d}],"
                           f"listing_ids:{{{'|'.join(chunk)}}}"),
                "metric": "LISTING_IMPRESSION_TOTAL,LISTING_VIEWS_TOTAL,TRANSACTION",
            },
            headers={"Authorization": f"Bearer {ebay_auth.access_token()}"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        keys = [m["key"] for m in data["header"]["metrics"]]
        with db.connect() as con:
            for rec in data.get("records", []):
                vals = dict(zip(keys, (m.get("value") or 0 for m in rec["metricValues"])))
                con.execute(
                    """INSERT OR REPLACE INTO traffic(item_id, impressions, views, transactions, period_days, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (rec["dimensionValues"][0]["value"], int(vals.get("LISTING_IMPRESSION_TOTAL", 0)),
                     int(vals.get("LISTING_VIEWS_TOTAL", 0)), int(vals.get("TRANSACTION", 0)), DAYS, now))
                count += 1
    return count


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def diagnose_all(market_checks: dict[str, dict] | None = None) -> dict[str, dict]:
    """Diagnose für alle aktiven Angebote. Schwellen relativ zum eigenen Bestand."""
    from . import market
    checks = market_checks if market_checks is not None else market.all_checks()
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        rows = con.execute(
            """SELECT l.item_id, l.start_time, l.watch_count, t.impressions, t.views, t.transactions
               FROM listings l LEFT JOIN traffic t USING(item_id) WHERE l.active = 1""").fetchall()
    with_data = [r for r in rows if r["impressions"] is not None]
    imps = [r["impressions"] for r in with_data]
    rates = [r["views"] / r["impressions"] for r in with_data if r["impressions"] >= 50]
    views = [r["views"] for r in with_data]
    low_imp = _quantile(imps, 0.25)
    med_rate = statistics.median(rates) if rates else 0.0
    high_views = _quantile(views, 0.75)

    out = {}
    for r in rows:
        age = (now - datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))).days if r["start_time"] else 0
        m = checks.get(r["item_id"])
        imp, vw = r["impressions"], r["views"]
        rate = (vw / imp) if imp else 0.0
        if age < 7:
            key = "neu"
        elif m and m.get("verdict") == "teuer":
            key = "zu_teuer"
        elif imp is not None and imp <= low_imp:
            key = "unsichtbar"
        elif imp is not None and imp >= 50 and rate < med_rate * 0.5:
            key = "klick"
        elif vw is not None and vw >= high_views and age >= 14 and not r["transactions"]:
            key = "kauf"
        else:
            key = "ok"
        icon, label, advice = DIAGNOSES[key]
        out[r["item_id"]] = {
            "key": key, "icon": icon, "label": label, "advice": advice,
            "impressions": imp, "views": vw, "view_rate": round(rate * 100, 1) if imp else None,
            "watchers": r["watch_count"], "days": age,
        }
    return out
