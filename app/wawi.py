"""Anbindung an die Warenwirtschaft (nur lesend!) und Margenrechnung.

Die WaWi-Datenbank wird ausschließlich im SQLite-Nur-Lese-Modus geöffnet.
Die Gebühren-/Gewinnformel ist aus der WaWi übernommen, damit beide gleich rechnen.
"""
import difflib
import json
import os
import re
import sqlite3

from . import db

WAWI_DB = os.getenv("WAWI_DB", "/home/papa/warenwirtschaft/warenwirtschaft.db")

# ── Gebühren & Gewinn (wie warenwirtschaft/app.py) ─────────────────────
EBAY_VARIABLE_FEE_RATES = {"Konsolen": 0.07, "Videospiele": 0.12, "Medien": 0.12,
                           "Zubehör": 0.12, "Elektro": 0.07, "Sonstiges": 0.14}
EBAY_PROFILE_RATES = {"5% begünstigt": 0.05, "5% – begünstigte Kategorie/Zustand": 0.05,
                      "5% gebraucht/refurbished": 0.05, "7% Konsolen/Elektro": 0.07,
                      "12% Standard": 0.12, "14% Sonstiges/Verschiedenes": 0.14}
DEFAULT_FEE_RATE = 0.12
FIXED_FEE_LOW, FIXED_FEE_HIGH, FIXED_FEE_THRESHOLD = 0.35, 0.45, 10.00
DIFF_TAX_RATE = 0.19
TARGET_PROFIT = 1.00  # Mindestgewinn je Artikel (wie in der WaWi)
DEFAULT_SHIPPING_COST = 1.90

SCHEMA = """
CREATE TABLE IF NOT EXISTS wawi_links (
    item_id    TEXT PRIMARY KEY,     -- eBay-Artikelnummer
    produktnr  TEXT NOT NULL,        -- WaWi-Produktnummer
    method     TEXT NOT NULL,        -- sku | ebay_nr | titel
    score      REAL,
    confirmed  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def money(v) -> float:
    if v is None:
        return 0.0
    s = str(v).replace("€", "").replace(".", "").replace(",", ".").strip() if "," in str(v) else str(v).replace("€", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def fee_rate(row: dict) -> float:
    profile = row.get("ebay_gebuehrenprofil", "")
    if profile in EBAY_PROFILE_RATES:
        return EBAY_PROFILE_RATES[profile]
    return EBAY_VARIABLE_FEE_RATES.get(row.get("kategorie", ""), DEFAULT_FEE_RATE)


def ebay_net_fee(total: float, rate: float) -> float:
    return total * rate + (FIXED_FEE_HIGH if total > FIXED_FEE_THRESHOLD else FIXED_FEE_LOW)


def profit(price: float, ek: float, rate: float, shipping_cost: float) -> dict:
    """Gewinn nach Gebühren, Porto und Differenzsteuer (§ 25a) – wie in der WaWi."""
    fee = ebay_net_fee(price, rate)
    tax = max(price - ek, 0.0) * DIFF_TAX_RATE / (1 + DIFF_TAX_RATE)
    return {"price": price, "ek": ek, "fee": round(fee, 2), "tax": round(tax, 2),
            "shipping": shipping_cost, "profit": round(price - ek - shipping_cost - fee - tax, 2)}


def min_price(ek: float, rate: float, shipping_cost: float, target: float) -> float:
    """Kleinster Preis mit mindestens `target` Gewinn (auch über die Fixgebühr-Schwelle hinweg stabil)."""
    for cents in range(max(0, int(ek * 100)), 1_000_000):
        vk = cents / 100
        if all(profit(p / 100, ek, rate, shipping_cost)["profit"] + 1e-6 >= target
               for p in range(cents, cents + 25)):
            return vk
    return 9999.99


# ── WaWi lesen ──────────────────────────────────────────────────────────

def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)


def available() -> bool:
    return os.path.exists(WAWI_DB)


def _rows() -> list[dict]:
    con = sqlite3.connect(f"file:{WAWI_DB}?mode=ro", uri=True)  # NUR LESEND
    try:
        return [json.loads(d) for (d,) in con.execute("SELECT data FROM rows")]
    finally:
        con.close()


def products() -> dict[str, dict]:
    """Alle WaWi-Artikel nach Produktnummer, mit den für uns relevanten Feldern."""
    out = {}
    for r in _rows():
        out[r["produktnr"]] = {
            "produktnr": r["produktnr"], "artikel": r.get("artikel", ""), "status": r.get("status", ""),
            "kategorie": r.get("kategorie", ""), "zustand": r.get("zustand", ""),
            "ek": money(r.get("ek")), "min_vk": money(r.get("min_vk")),
            "versand_kosten": money(r.get("versand_kosten")) if r.get("versand_kosten") else DEFAULT_SHIPPING_COST,
            "fee_rate": fee_rate(r), "ebay_artikelnr": r.get("ebay_artikelnr", ""),
            "lager_reihe": r.get("lager_reihe", ""),
        }
    return out


# ── Zuordnung eBay-Angebot ↔ WaWi-Artikel ──────────────────────────────

PLATFORM_RE = re.compile(r"\b(ps[1-5]|psx|psp|vita|xbox\s?(?:360|one|series)?|x360|switch|wii\s?u?|3ds|nds|ds|gamecube|n64|snes|nes|sega|dreamcast|saturn|mega\s?drive|gba|gameboy|pc)\b")


def _norm(s: str) -> str:
    s = s.lower().replace("xbox360", "xbox 360").replace("playstation ", "ps")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9äöüß ]", " ", s)).strip()


def _platforms(s: str) -> set[str]:
    return {m.replace(" ", "") for m in PLATFORM_RE.findall(_norm(s))}


def _score(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    pa, pb = _platforms(a), _platforms(b)
    if pa and pb and not (pa & pb):
        return 0.0  # verschiedene Plattformen – nie zuordnen
    return difflib.SequenceMatcher(None, na, nb).ratio()


def candidates(title: str, prods: dict[str, dict], n: int = 5) -> list[tuple[float, dict]]:
    pool = [p for p in prods.values() if p["status"] != "Verkauft"]
    scored = sorted(((_score(title, p["artikel"]), p) for p in pool), key=lambda x: -x[0])
    return [(round(s, 2), p) for s, p in scored[:n] if s > 0.3]


def auto_link() -> dict:
    """Ordnet aktive eBay-Angebote zu: SKU/eBay-Nr. sicher, Titel nur als Vorschlag (bestätigen)."""
    prods = products()
    by_item = {p["ebay_artikelnr"]: p for p in prods.values() if p["ebay_artikelnr"]}
    now = db.now_iso()
    stats = {"sku": 0, "ebay_nr": 0, "titel": 0, "offen": 0}
    with db.connect() as con:
        linked = {r["item_id"]: r for r in con.execute("SELECT * FROM wawi_links")}
        taken = {r["produktnr"] for r in linked.values() if r["confirmed"]}
        for l in con.execute("SELECT item_id, sku, title FROM listings WHERE active = 1").fetchall():
            if l["item_id"] in linked and linked[l["item_id"]]["confirmed"]:
                continue
            if l["sku"] and l["sku"] in prods:
                method, pnr, score, ok = "sku", l["sku"], 1.0, 1
            elif l["item_id"] in by_item:
                method, pnr, score, ok = "ebay_nr", by_item[l["item_id"]]["produktnr"], 1.0, 1
            else:
                cands = [(s, p) for s, p in candidates(l["title"], prods) if p["produktnr"] not in taken]
                if not cands or cands[0][0] < 0.55:
                    stats["offen"] += 1
                    continue
                method, pnr, score, ok = "titel", cands[0][1]["produktnr"], cands[0][0], 0
            stats[method] += 1
            con.execute(
                """INSERT OR REPLACE INTO wawi_links(item_id, produktnr, method, score, confirmed, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""", (l["item_id"], pnr, method, score, ok, now))
            if ok:
                taken.add(pnr)
    return stats


def link(item_id: str, produktnr: str | None) -> None:
    with db.connect() as con:
        if produktnr:
            con.execute(
                """INSERT OR REPLACE INTO wawi_links(item_id, produktnr, method, score, confirmed, updated_at)
                   VALUES (?, ?, COALESCE((SELECT method FROM wawi_links WHERE item_id = ?), 'manuell'), NULL, 1, ?)""",
                (item_id, produktnr, item_id, db.now_iso()))
        else:
            con.execute("DELETE FROM wawi_links WHERE item_id = ?", (item_id,))


def links(confirmed_only: bool = True) -> dict[str, str]:
    with db.connect() as con:
        q = "SELECT item_id, produktnr FROM wawi_links" + (" WHERE confirmed = 1" if confirmed_only else "")
        return {r["item_id"]: r["produktnr"] for r in con.execute(q)}


def for_items(item_ids: list[str]) -> dict[str, dict]:
    """WaWi-Daten zu eBay-Angeboten (nur bestätigte Zuordnungen)."""
    if not available():
        return {}
    ln = links()
    prods = products()
    return {i: prods[ln[i]] for i in item_ids if i in ln and ln[i] in prods}


def bundle_margin(item_ids: list[str], price: float, shipping_cost: float | None = None) -> dict:
    """Marge eines Bündels: EK-Summe, ein Porto statt vieler, Gebühren, Steuer, Mindestpreis."""
    data = for_items(item_ids)
    missing = [i for i in item_ids if i not in data]
    if missing:
        return {"complete": False, "missing": missing, "known": len(data)}
    ek = sum(p["ek"] for p in data.values())
    rate = max(p["fee_rate"] for p in data.values())
    single_shipping = sum(p["versand_kosten"] for p in data.values())
    ship = shipping_cost if shipping_cost is not None else max(p["versand_kosten"] for p in data.values())
    target = TARGET_PROFIT * len(item_ids)
    res = profit(price, ek, rate, ship)
    res.update(
        complete=True, target=target, min_price=min_price(ek, rate, ship, target),
        porto_saved=round(single_shipping - ship, 2), fee_rate=rate,
        sum_min_vk=round(sum(p["min_vk"] for p in data.values()), 2),
    )
    res["ok"] = res["profit"] + 1e-6 >= target
    return res
