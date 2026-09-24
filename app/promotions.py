"""Kombi-Rabatt (eBay Marketing API, Aktionstyp ORDER_DISCOUNT): „Kaufe 2, spare 10 %“ über mehrere Angebote.

Anders als ein Bündel bleiben die Einzelangebote mit Ranking und Beobachtern bestehen –
der Käufer stellt sich sein Paket selbst zusammen.
"""
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, ebay_auth, settings, wawi

BASE = f"{config.API_BASE}/sell/marketing/v1"
TYPES = {"ORDER_DISCOUNT": "Kombi-Rabatt", "CODED_COUPON": "Gutschein", "MARKDOWN_SALE": "Preisnachlass",
         "VOLUME_DISCOUNT": "Mengenrabatt"}
STATUS = {"DRAFT": "Entwurf", "SCHEDULED": "geplant", "RUNNING": "läuft", "PAUSED": "pausiert",
          "ENDED": "beendet", "INVALID": "ungültig"}


def _h() -> dict:
    return {"Authorization": f"Bearer {ebay_auth.access_token()}", "Content-Type": "application/json"}


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        try:
            errs = r.json().get("errors", [])
            msg = " | ".join(e.get("longMessage") or e.get("message", "") for e in errs)
        except Exception:
            msg = r.text[:300]
        raise RuntimeError(f"eBay: {msg or r.status_code}")
    return r


def list_all() -> list[dict]:
    r = _check(httpx.get(f"{BASE}/promotion", params={"marketplace_id": "EBAY_DE", "limit": 200},
                         headers=_h(), timeout=30))
    out = []
    for p in r.json().get("promotions", []):
        out.append({
            "id": p["promotionId"], "name": p.get("name", ""), "type": p.get("promotionType"),
            "type_label": TYPES.get(p.get("promotionType"), p.get("promotionType")),
            "status": p.get("promotionStatus"), "status_label": STATUS.get(p.get("promotionStatus"), p.get("promotionStatus")),
            "start": (p.get("startDate") or "")[:10], "end": (p.get("endDate") or "")[:10],
        })
    order = {"RUNNING": 0, "SCHEDULED": 1, "PAUSED": 2, "DRAFT": 3}
    return sorted(out, key=lambda p: (order.get(p["status"], 9), p["end"]), reverse=False)


def margin_check(item_ids: list[str], max_pct: float) -> dict[str, dict]:
    """Ergebnis je Artikel bei der höchsten Rabattstaffel (Ampel wie bei Bündeln, je Artikel)."""
    with db.connect() as con:
        prices = {r["item_id"]: r["price"] for r in con.execute(
            f"SELECT item_id, price FROM listings WHERE item_id IN ({','.join('?' * len(item_ids))})", item_ids)}
    data = wawi.for_items(item_ids)
    slow = set(wawi.slow_items(item_ids))
    target = settings.get("min_profit_per_item")
    max_loss = settings.get("max_loss_per_bundle")
    out = {}
    for iid in item_ids:
        w = data.get(iid)
        if not w or iid not in prices:
            out[iid] = {"level": "unbekannt"}
            continue
        price = round(prices[iid] * (1 - max_pct / 100), 2)
        # Bei 2+ Artikeln fällt das Porto nur einmal an – pro Artikel anteilig gerechnet (konservativ: halbes Porto)
        res = wawi.profit(price, w["ek"], w["fee_rate"], w["versand_kosten"] / 2)
        p = res["profit"] + 1e-6
        level = ("gut" if p >= target else "knapp" if p >= 0
                 else "abverkauf" if (p >= -max_loss and iid in slow) else "blockiert")
        out[iid] = {**res, "level": level}
    return out


def create_order_discount(name: str, description: str, item_ids: list[str], min_qty: int, pct: float,
                          days: int, image_url: str, draft: bool) -> str:
    """eBay erlaubt bei ORDER_DISCOUNT nur EINE Rabattstufe je Aktion (z. B. ab 2 Artikeln 15 %)."""
    start = datetime.now(timezone.utc) + timedelta(minutes=5)
    end = start + timedelta(days=days)
    rules = [{
        "discountSpecification": {"minQuantity": int(min_qty)},
        "discountBenefit": {"percentageOffOrder": f"{pct:g}"},
    }]
    body = {
        "name": name[:90],
        "description": description[:50],
        "marketplaceId": "EBAY_DE",
        "promotionStatus": "DRAFT" if draft else "SCHEDULED",
        "promotionType": "ORDER_DISCOUNT",
        "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "promotionImageUrl": image_url,
        "priority": "PRIORITY_1",
        "inventoryCriterion": {"inventoryCriterionType": "INVENTORY_BY_VALUE", "listingIds": item_ids[:500]},
        "discountRules": rules,
    }
    r = _check(httpx.post(f"{BASE}/item_promotion", json=body, headers=_h(), timeout=60))
    loc = r.headers.get("location", "")
    return loc.rsplit("/", 1)[-1] if loc else ""


def pause(promotion_id: str) -> None:
    _check(httpx.post(f"{BASE}/promotion/{promotion_id}/pause", headers=_h(), timeout=30))


def resume(promotion_id: str) -> None:
    _check(httpx.post(f"{BASE}/promotion/{promotion_id}/resume", headers=_h(), timeout=30))


def delete(promotion_id: str) -> None:
    """Nur für Entwürfe/geplante Aktionen, die noch nicht gelaufen sind."""
    _check(httpx.delete(f"{BASE}/item_promotion/{promotion_id}", headers=_h(), timeout=30))
