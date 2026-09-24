"""Anbindung an die Warenwirtschaft (nur lesend!) und Margenrechnung.

Die WaWi-Datenbank wird ausschließlich im SQLite-Nur-Lese-Modus geöffnet.
Die Gebühren-/Gewinnformel ist aus der WaWi übernommen, damit beide gleich rechnen.
"""
import difflib
import json
import os
import re
import sqlite3

from . import db, settings

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


# Wörter, die nichts über den Artikel aussagen (für die Eindeutigkeitsprüfung)
FILLER = {
    "pal", "de", "cib", "ovp", "neu", "sealed", "komplett", "getestet", "deutsch", "uncut", "usk", "fsk",
    "edition", "game", "spiel", "spiele", "the", "of", "and", "und", "der", "die", "das", "mit", "ohne",
    "anleitung", "handbuch", "hülle", "disc", "discs", "cd", "dvd", "version", "sony", "playstation",
    "microsoft", "nintendo", "sega", "xbox", "classic", "platinum", "essentials", "hits", "collection",
    "gut", "sehr", "zustand", "inkl", "nur", "b", "o", "a", "for", "für", "vom", "von", "im", "in",
}


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split()
            if len(t) >= 4 and t not in FILLER and not PLATFORM_RE.fullmatch(t) and not t.isdigit()}


def auto_link() -> dict:
    """Ordnet aktive eBay-Angebote WaWi-Artikeln zu.

    Sicher (ohne Rückfrage):
      - gleiche SKU bzw. gespeicherte eBay-Artikelnummer
      - „eindeutig“: Angebot und WaWi-Artikel sind gegenseitig der beste Treffer, die Plattform passt,
        und sie teilen ein markantes Wort, das es in der WaWi (offene Artikel) UND bei eBay nur je einmal gibt
        (z. B. „Giants“) – oder die Titel sind nahezu gleich.
    Alles andere wird nur vorgeschlagen (Titel-Ähnlichkeit) und muss bestätigt werden.
    """
    from collections import Counter

    prods = products()
    by_item = {p["ebay_artikelnr"]: p for p in prods.values() if p["ebay_artikelnr"]}
    now = db.now_iso()
    stats = {"sku": 0, "ebay_nr": 0, "eindeutig": 0, "titel": 0, "offen": 0}
    with db.connect() as con:
        linked = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM wawi_links")}
        listings = con.execute("SELECT item_id, sku, title FROM listings WHERE active = 1").fetchall()

    def save(iid, pnr, method, score, ok):
        with db.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO wawi_links(item_id, produktnr, method, score, confirmed, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""", (iid, pnr, method, score, ok, now))

    taken = {r["produktnr"] for r in linked.values() if r["confirmed"]}
    todo = []
    for l in listings:
        if l["item_id"] in linked and linked[l["item_id"]]["confirmed"]:
            continue
        if l["sku"] and l["sku"] in prods:
            save(l["item_id"], l["sku"], "sku", 1.0, 1); taken.add(l["sku"]); stats["sku"] += 1
        elif l["item_id"] in by_item:
            pnr = by_item[l["item_id"]]["produktnr"]
            save(l["item_id"], pnr, "ebay_nr", 1.0, 1); taken.add(pnr); stats["ebay_nr"] += 1
        else:
            todo.append(l)

    pool = [p for p in prods.values() if p["status"] != "Verkauft" and p["produktnr"] not in taken]
    # Wie oft kommt ein markantes Wort vor – in der WaWi und bei eBay?
    wawi_df = Counter(t for p in pool for t in _tokens(p["artikel"]))
    ebay_df = Counter(t for l in listings for t in _tokens(l["title"]))

    # Bewertungsmatrix; „Inseriert“ in der WaWi ist ein kleiner Pluspunkt
    scores: dict[str, list[tuple[float, dict]]] = {}
    for l in todo:
        row = [(_score(l["title"], p["artikel"]) + (0.03 if p["status"] == "Inseriert" else 0), p) for p in pool]
        scores[l["item_id"]] = sorted(row, key=lambda x: -x[0])[:5]
    best_listing_for: dict[str, tuple[float, str]] = {}
    for iid, row in scores.items():
        for s, p in row:
            if s > best_listing_for.get(p["produktnr"], (0, ""))[0]:
                best_listing_for[p["produktnr"]] = (s, iid)

    for l in todo:
        row = scores[l["item_id"]]
        if not row or row[0][0] < 0.45:
            stats["offen"] += 1
            continue
        s1, p1 = row[0]
        s2 = row[1][0] if len(row) > 1 else 0.0
        mutual = best_listing_for.get(p1["produktnr"], (0, ""))[1] == l["item_id"]
        rare = {t for t in _tokens(l["title"]) & _tokens(p1["artikel"]) if wawi_df[t] == 1 and ebay_df[t] == 1}
        sure = p1["produktnr"] not in taken and mutual and (rare or s1 >= 0.9 or (s1 >= 0.7 and s1 - s2 >= 0.25))
        if sure:
            save(l["item_id"], p1["produktnr"], "eindeutig", round(min(s1, 1.0), 2), 1)
            taken.add(p1["produktnr"])
            stats["eindeutig"] += 1
        elif s1 >= 0.55:
            save(l["item_id"], p1["produktnr"], "titel", round(min(s1, 1.0), 2), 0)
            stats["titel"] += 1
        else:
            stats["offen"] += 1
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
    target = settings.get("min_profit_per_item") * len(item_ids)
    max_loss = settings.get("max_loss_per_bundle")
    slow = slow_items(item_ids)
    is_slow = len(slow) * 2 >= len(item_ids)  # mind. die Hälfte sind Ladenhüter
    res = profit(price, ek, rate, ship)
    p = res["profit"] + 1e-6
    # Ampel: gut → knapp → abverkauf (nur Ladenhüter, kleines Minus) → blockiert
    if p >= target:
        level = "gut"
    elif p >= 0:
        level = "knapp"
    elif p >= -max_loss and is_slow:
        level = "abverkauf"
    else:
        level = "blockiert"
    res.update(
        complete=True, target=target, min_price=min_price(ek, rate, ship, target),
        floor_price=min_price(ek, rate, ship, -max_loss if is_slow else 0.0),
        porto_saved=round(single_shipping - ship, 2), fee_rate=rate,
        sum_min_vk=round(sum(p["min_vk"] for p in data.values()), 2),
        level=level, slow_count=len(slow), is_slow=is_slow, max_loss=max_loss,
    )
    res["ok"] = level == "gut"
    res["allowed"] = level != "blockiert"
    return res


def slow_items(item_ids: list[str]) -> list[str]:
    """Ladenhüter: lange online und höchstens 1 Beobachter."""
    from datetime import datetime, timezone
    days = settings.get("slow_days")
    now = datetime.now(timezone.utc)
    out = []
    with db.connect() as con:
        for iid in item_ids:
            r = con.execute("SELECT start_time, watch_count FROM listings WHERE item_id = ?", (iid,)).fetchone()
            if r and r["start_time"] and r["watch_count"] <= 1:
                age = (now - datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))).days
                if age >= days:
                    out.append(iid)
    return out


def missing_skus() -> list[tuple[str, str]]:
    """Fest zugeordnete Angebote, deren eBay-SKU fehlt oder nicht zur WaWi passt: [(item_id, produktnr)]"""
    with db.connect() as con:
        rows = con.execute(
            """SELECT w.item_id, w.produktnr FROM wawi_links w JOIN listings l USING(item_id)
               WHERE w.confirmed = 1 AND l.active = 1 AND (l.sku IS NULL OR l.sku != w.produktnr)""").fetchall()
    return [(r["item_id"], r["produktnr"]) for r in rows]


def write_skus() -> dict:
    from . import ebay_trading
    done, errors = 0, []
    for iid, pnr in missing_skus():
        try:
            ebay_trading.set_sku(iid, pnr)
            with db.connect() as con:
                con.execute("UPDATE listings SET sku = ? WHERE item_id = ?", (pnr, iid))
            done += 1
        except Exception as exc:
            errors.append(f"#{iid}: {exc}")
    return {"done": done, "errors": errors}


def sold_rows() -> list[dict]:
    """Verkaufte WaWi-Artikel mit tatsächlichem Verkaufspreis (vk)."""
    return [{"artikel": r.get("artikel", ""), "vk": money(r.get("vk")), "verkauft_am": r.get("verkauft_am", "")}
            for r in _rows() if r.get("status") == "Verkauft" and r.get("vk")]
