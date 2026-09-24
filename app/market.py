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
        if "own_sales" not in cols:
            con.execute("ALTER TABLE market_checks ADD COLUMN own_sales TEXT NOT NULL DEFAULT '[]'")
        if "excluded" not in cols:
            con.execute("ALTER TABLE market_checks ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        if "quick_price" not in cols:
            con.execute("ALTER TABLE market_checks ADD COLUMN quick_price REAL")


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


# Plattform im Titel erkennen: (Muster, Kurzname) – spezifische zuerst
TITLE_PLATFORMS = [
    (r"xbox\s?360|x360", "Xbox 360"), (r"xbox\s?one", "Xbox One"), (r"xbox\s?series", "Xbox Series X"),
    (r"ps\s?5|playstation\s?5", "PS5"), (r"ps\s?4|playstation\s?4", "PS4"), (r"ps\s?3|playstation\s?3", "PS3"),
    (r"ps\s?2|playstation\s?2", "PS2"), (r"ps\s?1|psx|ps\s?one|playstation\s?1\b|playstation\s?one", "PS1"),
    (r"\bpsp\b", "PSP"), (r"\bvita\b", "PS Vita"), (r"wii\s?u", "Wii U"), (r"\bwii\b", "Wii"),
    (r"\bswitch\b", "Switch"), (r"\b3ds\b", "3DS"), (r"nintendo\s?ds|\bnds\b|\bds\b", "DS"),
    (r"gamecube", "GameCube"), (r"\bxbox\b", "Xbox"),
]


def _title_platform(title: str) -> tuple[str | None, int]:
    """Plattform aus dem Titel und die Position, an der sie steht."""
    t = title.lower()
    best = None
    for pat, short in TITLE_PLATFORMS:
        m = re.search(pat, t)
        if m and (best is None or m.start() < best[1]):
            best = (short, m.start())
    return best if best else (None, -1)


def _name_from_title(title: str) -> str:
    """Spielname = Text vor der Plattform (bzw. danach, falls die Plattform vorne steht)."""
    plat, pos = _title_platform(title)
    part = title[:pos] if pos > 3 else title[pos:]
    if pos <= 3:  # Plattform steht vorne: Plattform und Trenner entfernen, bis zum nächsten Trenner lesen
        part = re.sub(r"^[^\-–|:]*?(ps\s?\d|xbox\s?\d*|playstation\s?\d?|nintendo\s?\w*|wii\s?u?|switch)\s*[\-–|:/]?\s*",
                      "", part, flags=re.I)
    part = re.split(r"\s[\-–|]\s|\(|\[|,", part)[0]
    return re.sub(r"\s+", " ", part).strip(" -–:|")


def build_query(details: dict) -> tuple[str, list[str], list[str]]:
    """Suchbegriff + Pflichtwörter + Plattform-Varianten für die Trefferprüfung."""
    spec = details["specifics"]
    name = (spec.get("Spielname") or [None])[0]
    plat = bundles.PLATFORM_SHORT.get((spec.get("Plattform") or [None])[0] or "")
    tplat, _ = _title_platform(details["title"])
    if not plat:
        plat = tplat or _title_platform(" ".join(spec.get("Plattform") or []))[0]
    if not name and tplat:
        name = _name_from_title(details["title"])   # kein Merkmal „Spielname“: aus dem Titel lesen
    if name and plat:
        query = f"{name} {plat}".strip()
        must = [w for w in _norm(name).split() if (len(w) > 1 or w.isdigit()) and w not in ("the", "of", "und", "and")]
        return query, must, PLATFORM_TOKENS.get(plat, [])
    # Kein Spiel (Zubehör o. ä.) – grobe Schätzung: Füllwörter weglassen, erste 4 aussagekräftige Wörter
    words = [w for w in details["title"].split()
             if not re.match(r"^(\d+-?tlg\.?|\d+x|set|neu|ovp|kompatibel|mit|für|und|&|/)$", w, re.I)][:4]
    query = " ".join(words)
    return query, [w for w in _norm(query).split() if len(w) > 2], []


# ── Vollständigkeit & Zustand: nur Gleiches mit Gleichem vergleichen ────

PARTIAL = re.compile(
    r"ohne\s+(anleitung|handbuch|booklet|beilage|hülle|huelle|ovp|cover)|\bo\.?\s?b\.?\b|nur\s+(disc|cd|dvd|modul|spiel|umd|cartridge)"
    r"|disc\s+only|\bloose\b|\blose\b|ohne\s+anl", re.I)
COMPLETE = re.compile(r"\bcib\b|komplett|vollständig|mit\s+(anleitung|handbuch|booklet)|\bovp\b|sealed|versiegelt", re.I)

COND_RANK_TEXT = [("defekt", 6), ("akzeptabel", 4), ("gut", 3)]  # „sehr gut“ wird vorher geprüft
COND_RANK_ID = {"1000": 0, "1500": 1, "1750": 1, "2000": 1, "2500": 1, "2750": 1, "4000": 2, "3000": 2,
                "5000": 3, "6000": 4, "7000": 6}


def completeness(title: str) -> str | None:
    if PARTIAL.search(title):
        return "teil"
    if COMPLETE.search(title):
        return "cib"
    return None


def _cond_rank(text: str | None) -> int | None:
    t = (text or "").lower()
    if not t:
        return None
    if "neu" in t and "neuwertig" not in t and "wie neu" not in t:
        return 0
    if "neuwertig" in t or "wie neu" in t:
        return 1
    if "sehr gut" in t:
        return 2
    for word, rank in COND_RANK_TEXT:
        if word in t:
            return rank
    return None


def _comparable(offer: dict, own_complete: str | None, own_rank: int | None) -> bool:
    theirs = completeness(offer["title"])
    if own_complete == "cib" and theirs == "teil":
        return False
    if own_complete == "teil" and theirs == "cib":
        return False
    r = _cond_rank(offer.get("condition"))
    if own_rank is not None and r is not None and r - own_rank >= 2:
        return False  # deutlich schlechterer Zustand
    return True


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
    # Nur vergleichbare Angebote (Vollständigkeit + Zustand); Käufer vergleichen den Gesamtpreis inkl. Versand
    own_complete = completeness(details["title"])
    own_rank = COND_RANK_ID.get(details.get("condition_id") or "")
    all_offers = sorted(seen.values(), key=lambda o: o["total"])
    offers = [o for o in all_offers if _comparable(o, own_complete, own_rank)]
    totals = [o["total"] for o in offers]
    # Marktpreis = Median der vergleichbaren Angebote; Schnellverkauf = unteres Drittel
    ref = statistics.median(totals) if totals else None
    quick = totals[int(len(totals) * 0.3)] if len(totals) >= 5 else (totals[0] if totals else None)
    own_ship = details.get("shipping_cost") or 0.0
    result = {
        "item_id": item_id,
        "checked_at": db.now_iso(),
        "query": query if plat_tokens else f"~{query}",   # „~“ = grobe Schätzung
        "found": len(offers),
        "avg5": round(ref, 2) if ref else None,            # Spaltenname historisch: hier steht der Marktpreis
        "min_price": totals[0] if totals else None,
        "median": round(statistics.median(totals), 2) if totals else None,
        "own_price": round(details["price"] + own_ship, 2),
        "own_shipping": own_ship,
        "samples": json.dumps(offers[:8], ensure_ascii=False),
        "own_sales": json.dumps(own_sales(item_id, details["title"]), ensure_ascii=False),
        "excluded": len(all_offers) - len(offers),
        "quick_price": round(quick, 2) if quick else None,
    }
    with db.connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO market_checks(item_id, checked_at, query, found, avg5, min_price, median,
                   own_price, own_shipping, samples, own_sales, excluded, quick_price)
               VALUES (:item_id, :checked_at, :query, :found, :avg5, :min_price, :median,
                   :own_price, :own_shipping, :samples, :own_sales, :excluded, :quick_price)""",
            result,
        )
    return describe(result, details["category_id"])


def own_sales(item_id: str, title: str) -> list[dict]:
    """Eigene Verkäufe desselben Artikels – echte Verkaufspreise aus WaWi und Bestellarchiv."""
    from . import ebay_orders, wawi
    out = []
    try:
        if wawi.available():
            link = wawi.links().get(item_id)
            prods = wawi.products()
            ref_name = prods[link]["artikel"] if link in prods else title
            for r in wawi.sold_rows():
                if r["vk"] > 0 and wawi._score(ref_name, r["artikel"]) >= 0.8:
                    out.append({"date": r["verkauft_am"], "price": r["vk"], "source": "WaWi", "title": r["artikel"]})
        for o in ebay_orders.all_orders():
            for li in o["items"]:
                if li["item_id"] == item_id or wawi._score(title, li["title"]) >= 0.85:
                    out.append({"date": o["date"], "price": li["price"] + li["shipping"], "source": "eBay", "title": li["title"]})
    except Exception:
        pass
    # Doppelte (gleicher Verkauf in WaWi und eBay-Archiv) grob entfernen
    seen, uniq = set(), []
    for s in sorted(out, key=lambda s: s["date"] or "", reverse=True):
        key = (s["date"], round(s["price"]))
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq[:10]


def describe(row: dict, category_id: str | None = None) -> dict:
    """Bewertung für die Oberfläche."""
    out = dict(row)
    out["samples"] = json.loads(row["samples"]) if isinstance(row["samples"], str) else row["samples"]
    sales = row.get("own_sales")
    out["own_sales"] = json.loads(sales) if isinstance(sales, str) else (sales or [])
    out["rough"] = row["query"].startswith("~")
    out["sold_url"] = sold_search_url(row["query"].lstrip("~"), category_id)
    sold_prices = [s["price"] for s in out["own_sales"]]
    out["sold_avg"] = round(statistics.mean(sold_prices), 2) if sold_prices else None
    ref = row["avg5"]
    if not ref:
        out.update(verdict="keine", label="Keine vergleichbaren Angebote gefunden", diff_pct=None, suggestion=None)
        return out
    diff = (row["own_price"] - ref) / ref * 100
    out["diff_pct"] = round(diff)
    ship = row.get("own_shipping") or 0.0
    quick = row.get("quick_price") or ref
    out["quick_price"] = quick
    out["suggestion"] = max(0.99, bundles.suggest_price(quick - ship, 3)) if quick - ship > 1 else None
    proven = out["sold_avg"] and row["own_price"] <= out["sold_avg"] * 1.1
    if proven:
        out.update(verdict="ok", label=f"bewährt – schon {len(sold_prices)}× für Ø {out['sold_avg']:.2f} € verkauft".replace(".", ","))
    elif diff > 20 and out["rough"]:
        out.update(verdict="unsicher", label=f"{diff:+.0f} % – unsicherer Vergleich, bitte prüfen")
    elif diff > 20:
        out.update(verdict="teuer", label=f"{diff:+.0f} % über Markt")
    elif row["own_price"] < quick * 0.85:
        # Aktive Angebote sind die unverkauften – „zu günstig“ erst unter dem günstigen Drittel
        out.update(verdict="guenstig", label=f"{diff:+.0f} % – sogar unter dem günstigen Drittel, evtl. Luft nach oben")
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
