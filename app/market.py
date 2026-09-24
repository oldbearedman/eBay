"""Marktwert-Check: vergleichbare aktuelle Angebote anderer Verkäufer (eBay Browse API).

Verkaufte Artikel sind über die offizielle API nicht abrufbar – dafür gibt es
einen Link auf die eBay-Suche „Verkaufte Artikel“.
"""
import json
import re
import statistics
from urllib.parse import quote_plus

import httpx

from . import bundles, config, db, ebay_auth, ebay_trading

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_checks (
    item_id     TEXT PRIMARY KEY,
    checked_at  TEXT NOT NULL,
    query       TEXT NOT NULL,
    found       INTEGER NOT NULL,
    avg5        REAL,
    min_price   REAL,
    median      REAL,
    own_price   REAL NOT NULL,      -- inkl. Versand
    own_shipping REAL NOT NULL DEFAULT 0,
    samples     TEXT NOT NULL      -- JSON: die 5 günstigsten Vergleichsangebote
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

EXCLUDE = re.compile(
    r"\b(paket|bundle|konvolut|sammlung|set|lot|leerh[üu]lle|nur\s+h[üu]lle|ohne\s+(spiel|disc|cd)|"
    r"h[üu]lle\s+ohne|nur\s+anleitung|l[öo]sungsbuch|guide|defekt)\b",
    re.I,
)

PLATFORM_TOKENS = {
    "PS1": ["ps1", "psx", "playstation 1", "playstation one"], "PS2": ["ps2", "playstation 2"],
    "PS3": ["ps3", "playstation 3"], "PS4": ["ps4", "playstation 4"], "PS5": ["ps5", "playstation 5"],
    "PSP": ["psp"], "PS Vita": ["vita"], "Xbox": ["xbox"], "Xbox 360": ["360"], "Xbox One": ["xbox one"],
    "Xbox Series X": ["series x", "series"], "Switch": ["switch"], "Wii": ["wii"], "Wii U": ["wii u"],
    "DS": ["ds"], "3DS": ["3ds"], "GameCube": ["gamecube", "game cube"], "PC": ["pc"],
}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(market_checks)")}
        if "own_shipping" not in cols:  # ältere Datenbank
            con.execute("ALTER TABLE market_checks ADD COLUMN own_shipping REAL NOT NULL DEFAULT 0")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9äöüß ]+", " ", s.lower())


def _setting(key: str) -> str | None:
    with db.connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def own_username() -> str:
    name = _setting("ebay_user")
    if not name:
        root = ebay_trading.call("GetUser", "")
        name = root.findtext("e:User/e:UserID", namespaces=ebay_trading.NS)
        with db.connect() as con:
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('ebay_user', ?)", (name,))
    return name


def build_query(details: dict) -> tuple[str, list[str], list[str]]:
    """Suchbegriff + Pflichtwörter + Plattform-Varianten für die Trefferprüfung."""
    spec = details["specifics"]
    name = (spec.get("Spielname") or [None])[0]
    plat = bundles.PLATFORM_SHORT.get((spec.get("Plattform") or [None])[0] or "")
    if name:
        query = f"{name} {plat or ''}".strip()
        must = [w for w in _norm(name).split() if len(w) > 1 or w.isdigit()]
        return query, must, PLATFORM_TOKENS.get(plat, [])
    # Freie Titel (grobe Schätzung): Mengen-/Füllwörter weglassen, erste 4 aussagekräftige Wörter
    words = [w for w in details["title"].split()
             if not re.match(r"^(\d+-?tlg\.?|\d+x|set|neu|ovp|kompatibel|mit|für|und|&|/)$", w, re.I)][:4]
    query = " ".join(words)
    return query, [w for w in _norm(query).split() if len(w) > 2], []


def _matches(title: str, must: list[str], plat_tokens: list[str]) -> bool:
    t = f" {_norm(title)} "
    if EXCLUDE.search(title):
        return False
    if plat_tokens and not any(f" {p} " in t or p in t.replace(" ", "") for p in plat_tokens):
        return False
    if not must:
        return True
    hits = sum(1 for w in must if f" {w} " in t)
    # Spielnamen: alle Wörter müssen vorkommen; freie Titel: mind. 60 %
    return hits == len(must) if plat_tokens else hits >= max(1, round(len(must) * 0.6))


def sold_search_url(query: str, category_id: str | None = None) -> str:
    url = f"https://www.ebay.de/sch/i.html?_nkw={quote_plus(query)}&LH_Sold=1&LH_Complete=1&_sop=13"
    return url + (f"&_sacat={category_id}" if category_id else "")


def check(item_id: str) -> dict:
    details = ebay_trading.item_details(item_id)
    query, must, plat_tokens = build_query(details)
    condition = "NEW" if details["condition_id"] in ("1000", "1500") else "USED"
    filters = [
        "buyingOptions:{FIXED_PRICE}",
        f"conditions:{{{condition}}}",
        "itemLocationCountry:DE",
        f"excludeSellers:{{{own_username()}}}",
    ]
    seen: dict[str, dict] = {}
    searches = []
    if details.get("ean") and details["ean"].isdigit():
        searches.append(({"gtin": details["ean"]}, False))       # exakt – keine Wortprüfung nötig
    searches.append(({"q": query, "category_ids": details["category_id"]}, True))
    for params, check_words in searches:
        r = httpx.get(
            f"{config.API_BASE}/buy/browse/v1/item_summary/search",
            params={**params, "filter": ",".join(filters), "limit": 100},
            headers={
                "Authorization": f"Bearer {ebay_auth.application_token()}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE",
            },
            timeout=30,
        )
        r.raise_for_status()
        for s in r.json().get("itemSummaries", []):
            if s["itemId"] in seen or EXCLUDE.search(s.get("title", "")):
                continue
            if check_words and not _matches(s.get("title", ""), must, plat_tokens):
                continue
            price = float(s["price"]["value"])
            ship_opts = s.get("shippingOptions") or []
            ship = float(ship_opts[0].get("shippingCost", {}).get("value", 0)) if ship_opts else 0.0
            seen[s["itemId"]] = {
                "title": s["title"], "price": price, "shipping": ship, "total": round(price + ship, 2),
                "url": s.get("itemWebUrl"), "condition": s.get("condition"),
                "image": (s.get("image") or {}).get("imageUrl"),
            }
    # Käufer vergleichen den Gesamtpreis inkl. Versand
    offers = sorted(seen.values(), key=lambda o: o["total"])
    cheapest = offers[:5]
    own_ship = details.get("shipping_cost") or 0.0
    result = {
        "item_id": item_id,
        "checked_at": db.now_iso(),
        "query": query,
        "found": len(offers),
        "avg5": round(statistics.mean(o["total"] for o in cheapest), 2) if cheapest else None,
        "min_price": cheapest[0]["total"] if cheapest else None,
        "median": round(statistics.median(o["total"] for o in offers), 2) if offers else None,
        "own_price": round(details["price"] + own_ship, 2),
        "own_shipping": own_ship,
        "samples": json.dumps(cheapest, ensure_ascii=False),
    }
    result["query"] = query if plat_tokens else f"~{query}"   # „~“ = grobe Schätzung
    with db.connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO market_checks(item_id, checked_at, query, found, avg5, min_price, median, own_price, own_shipping, samples)
               VALUES (:item_id, :checked_at, :query, :found, :avg5, :min_price, :median, :own_price, :own_shipping, :samples)""",
            result,
        )
    return describe(result, details["category_id"])


def describe(row: dict, category_id: str | None = None) -> dict:
    """Bewertung für die Oberfläche."""
    out = dict(row)
    out["samples"] = json.loads(row["samples"]) if isinstance(row["samples"], str) else row["samples"]
    out["rough"] = row["query"].startswith("~")
    out["sold_url"] = sold_search_url(row["query"].lstrip("~"), category_id)
    avg = row["avg5"]
    if not avg:
        out.update(verdict="keine", label="Keine vergleichbaren Angebote gefunden", diff_pct=None, suggestion=None)
        return out
    diff = (row["own_price"] - avg) / avg * 100
    out["diff_pct"] = round(diff)
    ship = row.get("own_shipping") or 0.0
    out["suggestion"] = max(0.99, bundles.suggest_price(avg - ship, 3)) if avg - ship > 1 else None
    if diff > 15:
        out.update(verdict="teuer", label=f"{diff:+.0f} % über Markt")
    elif diff < -15:
        out.update(verdict="guenstig", label=f"{diff:+.0f} % unter Markt – evtl. Luft nach oben")
    else:
        out.update(verdict="ok", label=f"marktgerecht ({diff:+.0f} %)")
    return out


def all_checks() -> dict[str, dict]:
    with db.connect() as con:
        rows = con.execute("SELECT * FROM market_checks").fetchall()
    return {r["item_id"]: describe(dict(r)) for r in rows}


# ── Alle Angebote prüfen (Hintergrund) ────────────────────────────────

job = {"running": False, "done": 0, "total": 0, "errors": 0}


def check_all() -> None:
    import logging
    log = logging.getLogger("ebay-manager")
    with db.connect() as con:
        ids = [r["item_id"] for r in con.execute(
            "SELECT item_id FROM listings WHERE active = 1 AND listing_type = 'FixedPriceItem'")]
    job.update(running=True, done=0, total=len(ids), errors=0)
    try:
        for iid in ids:
            try:
                check(iid)
            except Exception:
                job["errors"] += 1
                log.exception("Marktcheck %s fehlgeschlagen", iid)
            job["done"] += 1
    finally:
        job["running"] = False
