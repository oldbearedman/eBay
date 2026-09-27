"""Relist: ein zu teures Angebot neu einstellen – neuer Text von Claude, neuer Preis nach Markt, altes beenden.

Nutzt dieselbe Preis-, Versand- und Steuerlogik wie die Handy-Seite (handy.recalc / handy.verify).
"""
import json
import logging
import re
import threading

from . import ai, db, ebay_account, ebay_trading, handy, market, meta, wawi

log = logging.getLogger("ebay-manager")

SCHEMA = """
CREATE TABLE IF NOT EXISTS relists (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    old_item_id  TEXT NOT NULL,
    new_item_id  TEXT,
    status       TEXT NOT NULL DEFAULT 'vorbereiten',  -- vorbereiten | bereit | einstellen | online | fehler | verworfen
    step         TEXT,
    data         TEXT NOT NULL DEFAULT '{}',
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)
        con.execute("UPDATE relists SET status = 'fehler', error = 'Unterbrochen (Neustart)' WHERE status = 'vorbereiten'")
        con.execute("UPDATE relists SET status = 'bereit' WHERE status = 'einstellen' AND new_item_id IS NULL")


def _set(rid: int, **fields) -> None:
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    fields["updated_at"] = db.now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as con:
        con.execute(f"UPDATE relists SET {cols} WHERE id = ?", (*fields.values(), rid))


def get(rid: int) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM relists WHERE id = ?", (rid,)).fetchone()
    return {**dict(r), "data": json.loads(r["data"])} if r else None


def open_for(item_id: str) -> dict | None:
    """Offener Relist-Entwurf für dieses Angebot (damit nicht doppelt angelegt wird)."""
    with db.connect() as con:
        r = con.execute("SELECT id FROM relists WHERE old_item_id = ? AND status IN ('vorbereiten', 'bereit', 'einstellen') "
                        "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    return get(r["id"]) if r else None


def create(item_id: str) -> int:
    existing = open_for(item_id)
    if existing:
        return existing["id"]
    now = db.now_iso()
    with db.connect() as con:
        rid = con.execute("INSERT INTO relists(old_item_id, created_at, updated_at, step) VALUES (?, ?, ?, ?)",
                          (item_id, now, now, "Angebot wird gelesen …")).lastrowid
    threading.Thread(target=_prepare, args=(rid,), daemon=True).start()
    return rid


def discard(rid: int) -> None:
    r = get(rid)
    if r and r["status"] in ("bereit", "fehler"):
        _set(rid, status="verworfen")


# ── Vorbereiten ─────────────────────────────────────────────────────────

def _usk_from_specs(specs: dict) -> str:
    m = re.search(r"\d+", " ".join(specs.get("USK-Einstufung") or []))
    return m.group(0) if m and m.group(0) in ("0", "6", "12", "16", "18") else ""


def _prepare(rid: int) -> None:
    try:
        r = get(rid)
        item_id = r["old_item_id"]
        old = ebay_trading.item_details(item_id)
        it = ebay_trading.get_item(item_id)
        ns = ebay_trading.NS
        if it.findtext("e:SellingStatus/e:ListingStatus", namespaces=ns) != "Active":
            raise ValueError("Das Angebot ist nicht mehr aktiv.")
        qty = int(it.findtext("e:Quantity", default="1", namespaces=ns)) - \
            int(it.findtext("e:SellingStatus/e:QuantitySold", default="0", namespaces=ns))
        with db.connect() as con:
            row = con.execute("SELECT watch_count, start_time FROM listings WHERE item_id = ?", (item_id,)).fetchone()
        mt = meta.from_details(old)

        _set(rid, step="Marktpreise werden geprüft …")
        m = market.check(item_id)

        is_game = "videospiel" in (old["category_name"] or "").lower() and "zubehör" not in (old["category_name"] or "").lower()
        specs = old["specifics"]
        ident = {
            "artikel_typ": "videospiel" if is_game else "sonstiges", "name": mt.get("name") or old["title"],
            "plattform": (specs.get("Plattform") or [""])[0], "edition": "", "usk": _usk_from_specs(specs),
            "region": (specs.get("Regionalcode") or ["unbekannt"])[0], "sprache": "", "ean": old.get("ean") or "",
            "fremdfassung": False,
        }
        prof = ebay_account.profile_by_id(old["shipping_profile"]) or {}
        own_ship = prof.get("own_cost")
        if own_ship is None:
            own_ship = wawi.porto_rule(1, old["price"], bool(prof.get("age_check")))[0]
        d = {
            "old": {"item_id": item_id, "title": old["title"], "price": old["price"],
                    "shipping": old.get("shipping_cost") or 0.0, "total": round(old["price"] + (old.get("shipping_cost") or 0.0), 2),
                    "profile_name": old.get("shipping_profile_name"), "watchers": (row["watch_count"] if row else 0) or 0,
                    "start": (row["start_time"] if row else "") or "", "url": f"https://www.ebay.de/itm/{item_id}",
                    "image": (old["pictures"] or [None])[0], "diff_pct": m.get("diff_pct"), "label": m.get("label")},
            "ident": ident, "category_id": old["category_id"], "category_name": old["category_name"],
            "conditions": ebay_account.conditions(old["category_id"]), "condition_id": old["condition_id"],
            "wawi_pnr": wawi.links().get(item_id, ""), "sku": old.get("sku"), "quantity": max(1, qty),
            "ean": old.get("ean") if handy._valid_gtin(old.get("ean")) else None,
            "market": {"quick": m.get("quick_price"), "median": m.get("avg5"), "sold_avg": m.get("sold_avg"),
                       "found": m.get("found"), "sold_url": m.get("sold_url"), "rough": m.get("rough"),
                       "own_sales": m.get("own_sales") or [], "samples": (m.get("samples") or [])[:8]},
            "pictures": old["pictures"][:24], "location": old.get("location"), "postal_code": old.get("postal_code"),
            "country": old.get("country") or "DE", "return_profile": old["return_profile"],
            "payment_profile": old["payment_profile"],
            # Einstufung wie beim bisherigen Angebot (USK-Merkmal, Titel, Versand mit Altersprüfung)
            "usk18_fixed": wawi.is_usk18([item_id]) or bool(prof.get("age_check")),
        }
        if not is_game:   # Maße unbekannt → bisheriges Versandprofil behalten
            d["fixed_profile"] = {"own": own_ship, "kind": "wie bisher", "profile": old["shipping_profile"],
                                  "profile_name": old.get("shipping_profile_name") or prof.get("name", "?"),
                                  "buyer": prof.get("buyer_cost", old.get("shipping_cost") or 0.0),
                                  "age_check": bool(prof.get("age_check"))}

        _set(rid, step="Claude schreibt Titel & Beschreibung neu …")
        aspects = ebay_account.category_aspects(old["category_id"])
        facts = {"bisheriger_titel": old["title"], "zustand": old["condition_name"], "merkmale": specs,
                 "bisherige_beschreibung": ai.plain_text(old["description"], 2500)}
        text = ai.write_single_text(facts, "", old["condition_name"] or "", aspects, [])
        specifics = dict(specs)
        for k, v in text["specifics"].items():
            specifics.setdefault(k, v)
        d.update(title=text["title"], body=text["description"], specifics=specifics, text_by="claude")

        _set(rid, step="Preis & eBay-Prüfung …")
        handy.recalc(d)
        handy.verify(d)
        _set(rid, status="bereit", step=None, data=d)
    except Exception as exc:
        log.exception("Relist %s: Vorbereitung fehlgeschlagen", rid)
        _set(rid, status="fehler", step=None, error=str(exc)[:600])


# ── Ändern & Einstellen ─────────────────────────────────────────────────

def update(rid: int, form: dict) -> None:
    r = get(rid)
    if r["status"] != "bereit":
        raise ValueError("Dieser Relist kann gerade nicht geändert werden.")
    d = r["data"]
    d["title"] = ai.clean_title((form.get("title") or d["title"]).strip())
    if form.get("body"):
        d["body"] = form["body"]
    if form.get("condition_id") and any(c["id"] == form["condition_id"] for c in d["conditions"]):
        d["condition_id"] = form["condition_id"]
    d["tax_override"] = form.get("tax") or None
    d["price_choice"] = form.get("price_choice") or d.get("price_choice")
    custom = (form.get("custom_price") or "").replace(",", ".").strip()
    if d["price_choice"] == "eigen":
        if not custom:
            raise ValueError("Bitte einen eigenen Preis eintragen.")
        d["custom_price"] = max(0.99, round(float(custom), 2))
    handy.recalc(d)
    handy.verify(d)
    _set(rid, data=d)


def publish(rid: int, force: bool = False) -> dict:
    with db.connect() as con:
        claimed = con.execute("UPDATE relists SET status = 'einstellen' WHERE id = ? AND status = 'bereit'", (rid,)).rowcount
    if not claimed:
        raise ValueError("Dieser Relist ist nicht (mehr) bereit.")
    r = get(rid)
    d, old_id = r["data"], r["old_item_id"]
    try:
        handy.recalc(d)
        if not d["tax_mode"]:
            raise ValueError("Bitte die Steuerart wählen – in der WaWi ist sie nicht festgelegt.")
        if d["usk18"] and not d["shipping"]["age_check"]:
            raise ValueError("Ü18-Artikel, aber kein Versandprofil mit Altersprüfung.")
        if d["below_min"] and not force:
            raise ValueError(f"Preis liegt unter deinem Mindestpreis ({d['min_article']:.2f} €) – "
                             "Preis anheben oder „Trotzdem“ anhaken.".replace(".", ","))
        it = ebay_trading.get_item(old_id)
        ns = ebay_trading.NS
        if it.findtext("e:SellingStatus/e:ListingStatus", namespaces=ns) != "Active":
            raise ValueError("Das alte Angebot ist nicht mehr aktiv (evtl. gerade verkauft) – nichts geändert.")
        # 1. Neues Angebot – schlägt das fehl, bleibt das alte unverändert online
        res = ebay_trading.add_listing(handy.listing_data(d))
    except Exception:
        _set(rid, status="bereit")
        raise
    new_id = res["item_id"]
    problems = []
    # 2. Altes Angebot beenden
    try:
        ebay_trading.end_listing(old_id)
    except Exception as exc:
        problems.append(f"Altes Angebot #{old_id} konnte nicht beendet werden – bitte bei eBay beenden! ({exc})")
    # 3. Zuordnungen umziehen
    if d.get("wawi_pnr"):
        wawi.link(new_id, d["wawi_pnr"])
        wawi.link(old_id, None)
    with db.connect() as con:
        con.execute("UPDATE listings SET active = 0 WHERE item_id = ?", (old_id,))
    try:
        meta.save(meta.from_details(ebay_trading.item_details(new_id)))
    except Exception:
        log.exception("Steckbrief für %s nicht gespeichert", new_id)
    d["fees"], d["problems"] = res["fees"], problems
    _set(rid, status="online", new_item_id=new_id, data=d)
    threading.Thread(target=_after, args=(new_id,), daemon=True).start()
    return {"item_id": new_id, "problems": problems}


def _after(new_id: str) -> None:
    """Übersicht und Marktcheck für das neue Angebot aktualisieren."""
    from . import sync
    try:
        sync.run_sync()
        market.check(new_id)
    except Exception:
        log.exception("Nacharbeiten zu Relist %s", new_id)
