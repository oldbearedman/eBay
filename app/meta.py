"""Artikel-Steckbrief je Angebot: Plattform, Spielname, Genre, Vollständigkeit, Zustand.

Grundlage für gute Pakete (Spielreihen, gleiche Konsole, Themen-Pakete).
"""
import logging
import re
from collections import defaultdict

from . import bundles, db, ebay_trading, market

log = logging.getLogger("ebay-manager")

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_meta (
    item_id    TEXT PRIMARY KEY,
    platform   TEXT,
    name       TEXT,
    genre      TEXT,
    publisher  TEXT,
    complete   TEXT,          -- cib | teil | NULL
    usk        TEXT,
    age_ship   INTEGER,       -- 1 = Einzelangebot wird mit Altersprüfung verschickt („Alter“ KP)
    condition  TEXT,
    category   TEXT,
    updated_at TEXT NOT NULL
);
"""

# Wörter, die nicht zum Seriennamen gehören
SERIES_STOP = {"the", "der", "die", "das", "a", "an", "of", "und", "and", "edition", "game", "year",
               "original", "sony", "microsoft", "nintendo", "playstation", "xbox", "tom", "clancy", "clancys",
               "disney", "lego", "neu", "ovp"}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(item_meta)")}
        if "usk" not in cols:
            con.execute("ALTER TABLE item_meta ADD COLUMN usk TEXT")
        if "age_ship" not in cols:
            con.execute("ALTER TABLE item_meta ADD COLUMN age_ship INTEGER")


def from_details(d: dict) -> dict:
    spec = d["specifics"]
    name = (spec.get("Spielname") or [None])[0]
    platform = bundles.PLATFORM_SHORT.get((spec.get("Plattform") or [None])[0] or "")
    tplat, _ = market._title_platform(d["title"])
    platform = platform or tplat
    if not name and tplat:
        name = market._name_from_title(d["title"])
    return {
        "item_id": d["item_id"], "platform": platform, "name": name,
        "genre": ", ".join(spec.get("Genre") or []) or None,
        "publisher": (spec.get("Herausgeber") or spec.get("Marke") or [None])[0],
        "complete": market.completeness(d["title"]),
        "usk": ", ".join(spec.get("USK-Einstufung") or []) or None,
        "age_ship": _age_profile(d.get("shipping_profile")),
        "condition": d.get("condition_name"), "category": d.get("category_name"),
    }


def _age_profile(profile_id: str | None) -> int | None:
    """1, wenn das Versandprofil des Einzelangebots eine Altersprüfung hat („Alter“ KP)."""
    if not profile_id:
        return None
    from . import ebay_account
    p = ebay_account.profile_by_id(profile_id)
    return int(bool(p and p["age_check"])) if p else None


def save(m: dict) -> None:
    with db.connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO item_meta(item_id, platform, name, genre, publisher, complete, usk, age_ship, condition, category, updated_at)
               VALUES (:item_id, :platform, :name, :genre, :publisher, :complete, :usk, :age_ship, :condition, :category, :updated_at)""",
            {**m, "updated_at": db.now_iso()})


def refresh_missing(force: bool = False) -> int:
    """Steckbriefe für aktive Angebote nachladen – fehlende bzw. veraltete (ohne USK-Feld), mit force alle."""
    with db.connect() as con:
        ids = [r["item_id"] for r in con.execute(
            """SELECT l.item_id FROM listings l LEFT JOIN item_meta m USING(item_id)
               WHERE l.active = 1 AND (m.item_id IS NULL OR m.age_ship IS NULL OR ?)""", (int(force),))]
    for iid in ids:
        try:
            save(from_details(ebay_trading.item_details(iid)))
        except Exception:
            log.exception("Steckbrief %s nicht abrufbar", iid)
    return len(ids)


def all_meta() -> dict[str, dict]:
    with db.connect() as con:
        return {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM item_meta")}


def series_key(name: str | None) -> str | None:
    """Serienname: die ersten ein bis zwei markanten Wörter (z. B. „assassin creed“, „call duty“, „far cry“)."""
    if not name:
        return None
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("'s", "")).split()
             if w not in SERIES_STOP and not w.isdigit() and not re.fullmatch(r"[ivx]+", w)]
    if not words:
        return None
    return " ".join(words[:2]) if len(words[0]) <= 4 and len(words) > 1 else words[0]


# PS2 spielt PS1-Spiele ab – Pakete über beide Konsolen sind daher in Ordnung
COMPATIBLE = {frozenset({"PS1", "PS2"})}


def platforms_ok(platforms: set[str]) -> bool:
    platforms = {p for p in platforms if p}
    return len(platforms) <= 1 or frozenset(platforms) in COMPATIBLE


def groups(item_ids: list[str], metas: dict[str, dict]) -> dict:
    """Vorab gefundene Kandidaten: Spielreihen und Genre-Gruppen je Plattform."""
    by_series = defaultdict(list)
    by_genre = defaultdict(list)
    for iid in item_ids:
        m = metas.get(iid) or {}
        is_game = "videospiel" in (m.get("category") or "").lower() and "zubehör" not in (m.get("category") or "").lower()
        key = series_key(m.get("name")) if is_game else None
        if key:
            fam = "PS1/PS2" if m.get("platform") in ("PS1", "PS2") else (m.get("platform") or "?")
            by_series[(key, fam)].append(iid)
        for g in (m.get("genre") or "").split(", "):
            if g and m.get("platform"):
                by_genre[(m["platform"], g)].append(iid)
    return {
        # nur echte Reihen: mindestens zwei VERSCHIEDENE Spiele (Dubletten zählen nicht)
        "reihen": {f"{k} [{fam}]": v for (k, fam), v in by_series.items()
                   if len({(metas.get(i) or {}).get("name", "").lower() for i in v}) >= 2},
        "genres": {f"{plat} · {g}": v for (plat, g), v in by_genre.items() if len(v) >= 2},
    }
