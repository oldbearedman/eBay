"""Bündel: mehrere Angebote zu einem Kombi-Angebot zusammenfassen."""
import html
import json
import logging
import math
import re

from . import ai, collage, config, db, ebay_account, ebay_trading, settings, wawi

log = logging.getLogger("ebay-manager")

COLLAGE_DIR = config.DATA_DIR / "collagen"
COLLAGE_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bundles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT NOT NULL DEFAULT 'entwurf',  -- entwurf | online | aufgeloest | verkauft | beendet
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    new_item_id  TEXT,
    collage_url  TEXT,
    draft        TEXT NOT NULL,   -- JSON: Titel, Preis, Beschreibung, Merkmale …
    log          TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bundle_items (
    bundle_id    INTEGER NOT NULL REFERENCES bundles(id),
    position     INTEGER NOT NULL,
    item_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    price        REAL NOT NULL,
    quantity     INTEGER NOT NULL,
    sku          TEXT,
    image_url    TEXT,
    condition    TEXT,
    action       TEXT,            -- beendet | menge_reduziert
    relisted_id  TEXT,
    PRIMARY KEY (bundle_id, position)
);
"""

# Zustände von "am besten" nach "am schlechtesten" – das Bündel bekommt den schlechtesten
CONDITION_RANK = ["1000", "1500", "1750", "2000", "2010", "2020", "2030", "2500", "2750",
                  "4000", "5000", "6000", "3000", "7000"]

PLATFORM_SHORT = {
    "Sony PlayStation 1": "PS1", "Sony PlayStation 2": "PS2", "Sony PlayStation 3": "PS3",
    "Sony PlayStation 4": "PS4", "Sony PlayStation 5": "PS5", "Sony PSP": "PSP",
    "Sony PlayStation Vita": "PS Vita", "Microsoft Xbox": "Xbox", "Microsoft Xbox 360": "Xbox 360",
    "Microsoft Xbox One": "Xbox One", "Microsoft Xbox Series X": "Xbox Series X",
    "Nintendo Switch": "Switch", "Nintendo Wii": "Wii", "Nintendo Wii U": "Wii U",
    "Nintendo DS": "DS", "Nintendo 3DS": "3DS", "Nintendo GameCube": "GameCube", "PC": "PC",
}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)


def suggest_price(total: float, discount_pct: float) -> float:
    """Summe minus Rabatt, abgerundet auf ,49 bzw. ,99."""
    v = total * (1 - discount_pct / 100)
    return max(0.99, math.floor((v + 0.01) * 2) / 2 - 0.01)


def _short_name(d: dict) -> str:
    name = (d["specifics"].get("Spielname") or [None])[0]
    if name:
        plat = (d["specifics"].get("Plattform") or [None])[0]
        return f"{name} {PLATFORM_SHORT.get(plat, '')}".strip()
    return " ".join(d["title"].split()[:4])


def _suggest_title(details: list[dict]) -> str:
    names = [_short_name(d) for d in details]
    title = f"{len(details)}er Paket: " + " + ".join(names)
    if len(title) > 80:
        title = title[:77].rsplit(" ", 1)[0] + " …"
    return title


def _merge_specifics(details: list[dict], category_id: str) -> tuple[dict[str, list[str]], list[str]]:
    """Merkmale aller Artikel zusammenführen – unter Beachtung der eBay-Regeln der Kategorie.

    Merkmale, die nur einen Wert erlauben, aber bei den Artikeln verschieden sind,
    werden weggelassen (bzw. bei Pflichtmerkmalen auf den ersten Wert gekürzt).
    """
    merged: dict[str, list[str]] = {}
    for d in details:
        for name, values in d["specifics"].items():
            bucket = merged.setdefault(name, [])
            for v in values:
                if v not in bucket:
                    bucket.append(v)
    try:
        rules = ebay_account.category_aspects(category_id)
    except Exception:
        rules = {}
    notes = []
    for name in list(merged):
        values = merged[name]
        rule = rules.get(name, {"multi": False, "required": False, "free_text": True})
        if len(values) > 1 and not rule["multi"]:
            joined = " + ".join(values)
            if rule["required"] and rule.get("free_text") and len(joined) <= 65:
                merged[name] = [joined]
            elif rule["required"]:
                merged[name] = values[:1]
                notes.append(f"„{name}“ erlaubt nur einen Wert – Pflichtfeld, daher „{values[0]}“ übernommen.")
            else:
                del merged[name]
                notes.append(f"„{name}“ weggelassen (verschiedene Werte: {', '.join(values)}, eBay erlaubt nur einen).")
    for name, rule in rules.items():
        if rule["required"] and name not in merged:
            notes.append(f"Pflichtmerkmal „{name}“ fehlt – bitte ergänzen.")
    return merged, notes


def _description(details: list[dict]) -> str:
    rows = "".join(
        f"<li><b>{html.escape(d['title'])}</b> – Zustand: {html.escape(d['condition_name'] or '–')}</li>"
        for d in details
    )
    parts = [
        f"<h2>Paket mit {len(details)} Artikeln</h2>",
        "<p>Sie erhalten <b>alle</b> folgenden Artikel zusammen in einer Sendung:</p>",
        f"<ol>{rows}</ol>",
        "<p>Die Nummern entsprechen den Nummern auf dem ersten Bild.</p>",
        "<hr>",
    ]
    for i, d in enumerate(details, 1):
        parts.append(f"<h3>{i}. {html.escape(d['title'])}</h3>")
        parts.append(d["description"])
    return "\n".join(parts)


def compact_sku(skus: list[str]) -> str | None:
    """Alle Lagernummern in eine eBay-SKU (max. 50 Zeichen): gemeinsames Präfix nur einmal („VID-“)."""
    skus = [s for s in skus if s]
    if not skus:
        return None
    joined = "+".join(skus)
    if len(joined) <= 50:
        return joined
    prefix = skus[0].split("-")[0] + "-"
    short = "+".join([skus[0]] + [s[len(prefix):] if s.startswith(prefix) else s for s in skus[1:]])
    return short if len(short) <= 50 else f"{skus[0]}+{len(skus) - 1}weitere"[:50]


def _sku(details: list[dict]) -> str | None:
    return compact_sku([d["sku"] for d in details if d.get("sku")])


def pick_profile(n: int, value: float, usk18: bool, current_id: str | None = None) -> dict | None:
    """Versandprofil nach deiner Regel (wawi.porto_rule):
    Ü18 → immer „Alter“ KP (Altersprüfung, für den Käufer kostenlos → Preis inkl. Versand);
    1 Spiel / 2 Spiele bis Wertgrenze → Brief; bis N Artikel & Wert Y → Kleinpaket; darüber → Paket kostenlos."""
    profs = ebay_account.shipping_profiles()
    fast = [p for p in profs if (p.get("handling_days") or 0) <= 3]
    cur = next((p for p in profs if p["id"] == current_id), None)
    _, kind = wawi.porto_rule(n, value, usk18)
    if usk18:
        age = sorted((p for p in fast if p["age_check"]), key=lambda p: p["buyer_cost"])
        if age:
            cur = age[0]
    elif "Paket" in kind and not kind.startswith("Kleinpaket"):
        free = [p for p in fast if p["buyer_cost"] == 0 and "paket" in p["service"].lower() and not p["age_check"]]
        if free:
            cur = free[0]
    elif kind.startswith(("1 Spiel", "2 Spiele")):
        # Brief-Versand: bisheriges Profil behalten, falls es ein Brief ist, sonst ein Brief-Profil
        if not (cur and "brief" in cur["service"].lower()):
            brief = [p for p in fast if "brief" in p["service"].lower() and not p["age_check"]]
            if brief:
                cur = brief[0]
    elif not cur or (cur.get("own_cost") or 0) < settings.get("porto_kp") or cur["age_check"]             or (cur.get("own_cost") or 0) >= settings.get("porto_paket"):
        # Kleinpaket-Profil: Versandart kostet mind. Kleinpaket-Porto, aber weniger als ein Paket
        kp = sorted((p for p in fast if not p["age_check"]
                     and settings.get("porto_kp") <= (p.get("own_cost") or 0) < settings.get("porto_paket")),
                    key=lambda p: p.get("own_cost") or 99)
        if kp:
            cur = kp[0]
    return cur


def create_draft(item_ids: list[str], price: float | None = None, hint: str | None = None) -> int:
    details = [ebay_trading.item_details(i) for i in item_ids]
    for d in details:
        if d["listing_type"] != "FixedPriceItem":
            raise ValueError(f"„{d['title']}“ ist eine Auktion und kann nicht gebündelt werden.")
    total = sum(d["price"] for d in details)
    # Vergleichsbasis für den Käufer: Einzelpreise INKL. ihres jeweiligen Versands
    total_incl = sum(d["price"] + (d.get("shipping_cost") or 0.0) for d in details)
    worst = max(details, key=lambda d: CONDITION_RANK.index(d["condition_id"])
                if d["condition_id"] in CONDITION_RANK else len(CONDITION_RANK))
    first = details[0]
    specifics, notes = _merge_specifics(details, first["category_id"])
    # Ü18: USK-Merkmal, Titel – oder das Einzelangebot geht schon mit Altersprüfung („Alter“ KP) raus
    try:
        age_profiles = {p["id"] for p in ebay_account.shipping_profiles() if p["age_check"]}
    except Exception:
        age_profiles = set()
    usk18 = any(("18" in " ".join(d["specifics"].get("USK-Einstufung", [])))
                or re.search(r"\b(USK|FSK)\s?18\b|ab 18", d["title"], re.I)
                or d.get("shipping_profile") in age_profiles for d in details)
    # Versandprofil nach deiner Regel (siehe pick_profile)
    ship_id = first["shipping_profile"]
    value = price or suggest_price(total, 10)
    try:
        cur = pick_profile(len(details), value, bool(usk18), ship_id)
        ship_id = cur["id"] if cur else ship_id
        buyer_cost = cur["buyer_cost"] if cur else 0.0
    except Exception:
        buyer_cost = 0.0
    # Vorschlagspreise sind Gesamtpreise (inkl. Versand) → Artikelpreis = Gesamt − Versand für den Käufer
    if price:
        article_price = max(0.99, round(price - buyer_cost, 2))
        planned_total = round(price, 2)
    else:
        article_price = suggest_price(total, 10)
        planned_total = round(article_price + buyer_cost, 2)
    draft = {
        "title": _suggest_title(details),
        "discount": round((1 - planned_total / total_incl) * 100) if total_incl else 10,
        "total": round(total, 2),
        "total_incl": round(total_incl, 2),
        "price": article_price,
        "planned_total": planned_total,
        "usk18": bool(usk18),
        "description": _description(details),
        "category_id": first["category_id"],
        "categories": sorted({(d["category_id"], d["category_name"]) for d in details}),
        "condition_id": worst["condition_id"],
        "conditions": sorted({(d["condition_id"], d["condition_name"]) for d in details}),
        "shipping_profile": ship_id,
        "return_profile": first["return_profile"],
        "payment_profile": first["payment_profile"],
        "location": first["location"],
        "postal_code": first["postal_code"],
        "country": first["country"],
        "sku": _sku(details),
        "specifics": specifics,
        "specifics_notes": notes,
        "extra_pictures": [p for d in details for p in d["pictures"]],
        "sources": [
            {"title": d["title"], "condition": d["condition_name"] or "",
             "specifics": d["specifics"], "description": ai.plain_text(d["description"])}
            for d in details
        ],
        "text_by": "vorlage",
        "hint": hint,
    }
    if ai.enabled():
        try:
            draft.update(ai.write_bundle_text(draft["sources"], draft["price"], hint, _shipping_note(ship_id)),
                         text_by="claude")
        except Exception as exc:
            log.exception("Claude-Text fehlgeschlagen")
            draft["ai_error"] = str(exc)[:300]
    # Hauptbild je Artikel (Fallback: Vorschaubild aus der Übersicht) – Collage VOR dem Speichern bauen
    with db.connect() as con:
        thumbs = {r["item_id"]: r["image_url"] for r in con.execute(
            f"SELECT item_id, image_url FROM listings WHERE item_id IN ({','.join('?' * len(item_ids))})", item_ids)}
    main_pics = [(d["pictures"][0] if d["pictures"] else d["image_url"]) or thumbs.get(d["item_id"]) for d in details]
    img = collage.build(main_pics)
    now = db.now_iso()
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO bundles(created_at, updated_at, draft) VALUES (?, ?, ?)",
            (now, now, json.dumps(draft, ensure_ascii=False)),
        )
        bundle_id = cur.lastrowid
        for pos, d in enumerate(details, 1):
            con.execute(
                """INSERT INTO bundle_items(bundle_id, position, item_id, title, price, quantity, sku, image_url, condition)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bundle_id, pos, d["item_id"], d["title"], d["price"], d["quantity"], d["sku"],
                 main_pics[pos - 1], d["condition_name"]),
            )
    (COLLAGE_DIR / f"{bundle_id}.jpg").write_bytes(img)
    return bundle_id


def rewrite_text(bundle_id: int) -> None:
    """Titel und Beschreibung neu von Claude schreiben lassen."""
    b = get(bundle_id)
    d = b["draft"]
    d.update(ai.write_bundle_text(d["sources"], d["price"], d.get("hint"), _shipping_note(d["shipping_profile"])),
             text_by="claude")
    d.pop("ai_error", None)
    d.pop("text_outdated", None)
    _update(bundle_id, draft=json.dumps(d, ensure_ascii=False))


def get(bundle_id: int) -> dict | None:
    with db.connect() as con:
        b = con.execute("SELECT * FROM bundles WHERE id = ?", (bundle_id,)).fetchone()
        if not b:
            return None
        items = con.execute(
            "SELECT * FROM bundle_items WHERE bundle_id = ? ORDER BY position", (bundle_id,)
        ).fetchall()
    return {**dict(b), "draft": json.loads(b["draft"]), "items": [dict(i) for i in items]}


def all_bundles() -> list[dict]:
    with db.connect() as con:
        rows = con.execute("SELECT id FROM bundles ORDER BY id DESC").fetchall()
    return [get(r["id"]) for r in rows]


def parse_specifics(text: str) -> dict[str, list[str]]:
    """Textfeld „Name: Wert1 | Wert2“ (eine Zeile pro Merkmal) → dict."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if ":" in line:
            name, values = line.split(":", 1)
            vals = [v.strip() for v in values.split("|") if v.strip()]
            if name.strip() and vals:
                out[name.strip()] = vals
    return out


def format_specifics(specs: dict[str, list[str]]) -> str:
    return "\n".join(f"{k}: {' | '.join(v)}" for k, v in specs.items())


def save_draft(bundle_id: int, form: dict) -> dict:
    b = get(bundle_id)
    d = b["draft"]
    d["title"] = form["title"].strip()[:80]
    d["price"] = round(float(form["price"].replace(",", ".")), 2)
    try:
        d["discount"] = float(str(form.get("discount") or d["discount"]).replace(",", "."))
    except ValueError:
        pass
    d["description"] = form["description"]
    d["category_id"] = form["category_id"]
    d["condition_id"] = form["condition_id"]
    d["allow_below_min"] = form.get("allow_below_min") == "on"
    if form["shipping_profile"] != d.get("shipping_profile") and d.get("text_by") == "claude":
        d["text_outdated"] = True   # Versand geändert → Text evtl. mit falscher Versandangabe
    d["shipping_profile"] = form["shipping_profile"]
    d["return_profile"] = form.get("return_profile") or d["return_profile"]
    d["payment_profile"] = form.get("payment_profile") or d["payment_profile"]
    d["specifics"] = parse_specifics(form["specifics"])
    d["include_originals"] = form.get("include_originals") == "on"
    porto = (form.get("porto") or "").strip()
    d["porto"] = round(float(porto.replace(",", ".")), 2) if porto else None   # leer = automatisch nach Staffel
    _update(bundle_id, draft=json.dumps(d, ensure_ascii=False))
    return d


def _update(bundle_id: int, **fields) -> None:
    fields["updated_at"] = db.now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as con:
        con.execute(f"UPDATE bundles SET {cols} WHERE id = ?", (*fields.values(), bundle_id))


def _append_log(bundle_id: int, line: str) -> None:
    with db.connect() as con:
        con.execute("UPDATE bundles SET log = log || ? WHERE id = ?", (f"{db.now_iso()[:16]} {line}\n", bundle_id))


def _listing_data(b: dict, collage_url: str | None) -> dict:
    d = dict(b["draft"])
    pictures = [collage_url] if collage_url else []
    if d.get("include_originals", True):
        pictures += d["extra_pictures"]
    d["pictures"] = pictures[:24] or d["extra_pictures"][:24]
    return d


def verify(bundle_id: int) -> dict:
    b = get(bundle_id)
    return ebay_trading.verify_listing(_listing_data(b, b["collage_url"]))


def publish(bundle_id: int) -> dict:
    """Stellt das Bündel ein und nimmt danach die Einzelartikel heraus."""
    b = get(bundle_id)
    if b["status"] != "entwurf":
        raise ValueError("Dieses Bündel ist kein Entwurf mehr.")
    prof = ebay_account.profile_by_id(b["draft"]["shipping_profile"])
    if b["draft"].get("usk18") and prof is not None and not prof["age_check"] and not b["draft"].get("allow_below_min"):
        raise ValueError("Das Paket enthält ein USK-18-Spiel – bitte ein Versandprofil mit Altersprüfung "
                         "(z. B. „Alter“ KP) wählen oder „Trotzdem einstellen“ anhaken.")
    m = wawi.bundle_margin([it["item_id"] for it in b["items"]], b["draft"]["price"], b["draft"].get("porto"),
                           charged=(prof or {}).get("buyer_cost", 0.0))
    if m.get("complete") and not m["allowed"] and not b["draft"].get("allow_below_min"):
        if m.get("mixed_tax"):
            raise ValueError("Das Paket mischt differenz- und regelbesteuerte Artikel. "
                             "Bitte trennen oder „Trotzdem einstellen“ anhaken.")
        raise ValueError(
            f"Zu viel Minus: Gewinn {m['profit']:.2f} € – unterste Grenze für dieses Bündel ist {m['floor_price']:.2f} €. "
            "Preis anheben oder „Trotzdem einstellen“ anhaken.".replace(".", ","))
    if m.get("complete") and m["level"] == "abverkauf":
        _append_log(bundle_id, f"Abverkauf: Ladenhüter-Bündel mit {m['profit']:.2f} € Ergebnis")

    # 1. Sind alle Einzelartikel noch verfügbar?
    for it in b["items"]:
        cur = ebay_trading.item_details(it["item_id"])
        if cur["quantity"] < 1:
            raise ValueError(f"„{it['title']}“ ist nicht mehr verfügbar (verkauft oder beendet).")
        it["quantity"] = cur["quantity"]

    # 2. Collage hochladen (einmalig)
    collage_url = b["collage_url"]
    if not collage_url:
        collage_url = ebay_trading.upload_picture((COLLAGE_DIR / f"{bundle_id}.jpg").read_bytes(), f"paket-{bundle_id}.jpg")
        _update(bundle_id, collage_url=collage_url)

    # 3. Bündel einstellen – schlägt das fehl, bleibt alles wie es war
    result = ebay_trading.add_listing(_listing_data(b, collage_url))
    _update(bundle_id, status="online", new_item_id=result["item_id"])
    _append_log(bundle_id, f"Bündel eingestellt als #{result['item_id']}")

    # 4. Einzelartikel herausnehmen
    problems = []
    for it in b["items"]:
        try:
            if it["quantity"] > 1:
                ebay_trading.set_quantity(it["item_id"], it["quantity"] - 1)
                action = "menge_reduziert"
            else:
                ebay_trading.end_listing(it["item_id"])
                action = "beendet"
            with db.connect() as con:
                con.execute("UPDATE bundle_items SET action = ? WHERE bundle_id = ? AND item_id = ?",
                            (action, bundle_id, it["item_id"]))
            _append_log(bundle_id, f"#{it['item_id']}: {action}")
        except Exception as exc:
            problems.append(f"„{it['title']}“: {exc}")
            _append_log(bundle_id, f"#{it['item_id']}: FEHLER {exc}")

    # 5. In der WaWi als Konvolut zusammenfassen (Block „EB<Nr.>“), wenn alle Artikel sicher zugeordnet sind
    if settings.get("wawi_konvolut") and wawi.available():
        links = wawi.links()
        pnrs = [links.get(it["item_id"]) for it in b["items"]]
        if all(pnrs):
            try:
                block = wawi.free_block_name(f"EB{bundle_id}")
                ok = wawi.set_block(pnrs, block)
                d = get(bundle_id)["draft"]
                d["wawi_block"] = block
                _update(bundle_id, draft=json.dumps(d, ensure_ascii=False))
                _append_log(bundle_id, f"WaWi: Konvolut „{block}“ gesetzt ({len(ok)}/{len(pnrs)} Artikel)")
                if len(ok) != len(pnrs):
                    problems.append(f"WaWi-Konvolut nur bei {len(ok)} von {len(pnrs)} Artikeln gesetzt")
            except Exception as exc:
                problems.append(f"WaWi-Konvolut nicht gesetzt: {exc}")
                _append_log(bundle_id, f"WaWi: FEHLER beim Konvolut: {exc}")
        else:
            _append_log(bundle_id, "WaWi: kein Konvolut – nicht alle Artikel sind einem WaWi-Artikel zugeordnet")
    return {**result, "problems": problems}


def dissolve(bundle_id: int) -> list[str]:
    """Bündel beenden und die Einzelartikel wieder einstellen."""
    b = get(bundle_id)
    if b["status"] != "online":
        raise ValueError("Nur Bündel, die online sind, können aufgelöst werden.")
    ebay_trading.end_listing(b["new_item_id"])
    _append_log(bundle_id, f"Bündel #{b['new_item_id']} beendet")
    problems = []
    for it in b["items"]:
        try:
            if it["action"] == "beendet":
                new_id = ebay_trading.relist(it["item_id"])
                with db.connect() as con:
                    con.execute("UPDATE bundle_items SET relisted_id = ? WHERE bundle_id = ? AND item_id = ?",
                                (new_id, bundle_id, it["item_id"]))
                _append_log(bundle_id, f"#{it['item_id']} wieder eingestellt als #{new_id}")
            elif it["action"] == "menge_reduziert":
                cur = ebay_trading.item_details(it["item_id"])
                ebay_trading.set_quantity(it["item_id"], cur["quantity"] + 1)
                _append_log(bundle_id, f"#{it['item_id']}: Menge wieder +1")
        except Exception as exc:
            problems.append(f"„{it['title']}“: {exc}")
            _append_log(bundle_id, f"#{it['item_id']}: FEHLER beim Wiedereinstellen: {exc}")
    # WaWi-Konvolut wieder auflösen
    block = b["draft"].get("wawi_block")
    if block and wawi.available():
        links = wawi.links()
        pnrs = [p for p in (links.get(it["item_id"]) for it in b["items"]) if p]
        try:
            wawi.set_block(pnrs, "")
            _append_log(bundle_id, f"WaWi: Konvolut „{block}“ aufgelöst")
        except Exception as exc:
            problems.append(f"WaWi-Konvolut nicht aufgelöst: {exc}")
    _update(bundle_id, status="aufgeloest")
    return problems


def delete_draft(bundle_id: int) -> None:
    b = get(bundle_id)
    if b and b["status"] == "entwurf":
        with db.connect() as con:
            con.execute("DELETE FROM bundle_items WHERE bundle_id = ?", (bundle_id,))
            con.execute("DELETE FROM bundles WHERE id = ?", (bundle_id,))
        (COLLAGE_DIR / f"{bundle_id}.jpg").unlink(missing_ok=True)


def check_online_bundles(active_ids: set[str]) -> None:
    """Nach jedem Abgleich: Online-Bündel, die nicht mehr aktiv sind, als verkauft/beendet markieren."""
    with db.connect() as con:
        rows = con.execute("SELECT id, new_item_id FROM bundles WHERE status = 'online'").fetchall()
    for r in rows:
        if r["new_item_id"] in active_ids:
            continue
        try:
            it = ebay_trading.get_item(r["new_item_id"])
            sold = int(it.findtext("e:SellingStatus/e:QuantitySold", default="0", namespaces=ebay_trading.NS))
        except Exception:
            sold = 0
        _update(r["id"], status="verkauft" if sold else "beendet")
        _append_log(r["id"], "verkauft 🎉" if sold else "bei eBay beendet")



def _shipping_note(profile_id: str) -> str | None:
    """Versandangabe für Claude – so, wie der Käufer sie im Angebot sieht."""
    p = ebay_account.profile_by_id(profile_id)
    if not p:
        return None
    if p["buyer_cost"]:
        note = f"Käufer zahlt {p['buyer_cost']:.2f} € Versand".replace(".", ",")
    else:
        note = "kostenlos (versandkostenfrei)"
    return note + (", Versand mit Altersprüfung (Übergabe nur an Volljährige)" if p["age_check"] else "")
