"""Handy-Einstellen: Fotos + kurze Zustandsnotiz → Claude erkennt den Artikel → Marktpreis, Versand, Steuer
→ fertiges eBay-Angebot zur Vorschau → mit einem Tipp online (und in der WaWi auf „Inseriert“)."""
import io
import json
import logging
import math
import statistics
import threading

from PIL import Image, ImageOps

from . import ai, bundles, config, db, ebay_account, ebay_trading, market, settings, wawi

log = logging.getLogger("ebay-manager")

PHOTO_DIR = config.DATA_DIR / "handy"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS handy_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'erkennen',  -- erkennen | bestaetigen | analyse | bereit | einstellen | online | fehler | verworfen
    step        TEXT,
    note        TEXT NOT NULL DEFAULT '',
    photos      INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL DEFAULT '{}',
    item_id     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

VIDEOGAME_CAT = "139973"
CONSOLE_CAT = "139971"
MAX_PHOTOS = 12

# Claudes Zustandsstufen → eBay-Zustandsname (je Kategorie wird der erlaubte Wert gesucht)
CONDITION_ORDER = ["neu", "neuwertig", "sehr_gut", "gut", "akzeptabel", "defekt"]
CONDITION_NAMES = {"neu": "neu", "neuwertig": "neuwertig", "sehr_gut": "sehr gut", "gut": "gut",
                   "akzeptabel": "akzeptabel", "defekt": "defekt"}

TAX_NOTE_25A = "Differenzbesteuert nach § 25a UStG – die Umsatzsteuer wird nicht gesondert ausgewiesen."
AGE_NOTE = "USK ab 18: Versand mit Altersprüfung – Übergabe nur an Personen ab 18 Jahren."
NO_USK_NOTE = ("Dieser Artikel trägt keine deutsche USK-Kennzeichnung. Versand daher nur mit Altersprüfung – "
               "Übergabe nur an Personen ab 18 Jahren.")


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)
        if "pictures" not in {r["name"] for r in con.execute("PRAGMA table_info(handy_items)")}:
            con.execute("ALTER TABLE handy_items ADD COLUMN pictures TEXT")   # eBay-Bild-Adressen (JSON)
        # Beim Neustart abgebrochene Analysen/Einstellvorgänge nicht ewig „laufend“ zeigen
        con.execute("UPDATE handy_items SET status = 'fehler', error = 'Unterbrochen (Neustart) – bitte neu analysieren.' "
                    "WHERE status IN ('analyse', 'erkennen')")
        con.execute("UPDATE handy_items SET status = 'bereit' WHERE status = 'einstellen' AND item_id IS NULL")


# ── Speicher ────────────────────────────────────────────────────────────

def _set(hid: int, **fields) -> None:
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    fields["updated_at"] = db.now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as con:
        con.execute(f"UPDATE handy_items SET {cols} WHERE id = ?", (*fields.values(), hid))


def get(hid: int) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM handy_items WHERE id = ?", (hid,)).fetchone()
    return {**dict(r), "data": json.loads(r["data"])} if r else None


def recent(limit: int = 30) -> list[dict]:
    with db.connect() as con:
        rows = con.execute("SELECT id FROM handy_items WHERE status != 'verworfen' ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
    return [get(r["id"]) for r in rows]


def photo_path(hid: int, n: int):
    return PHOTO_DIR / str(hid) / f"{n}.jpg"


def _prepare(raw: bytes, size: int) -> bytes:
    """Foto drehen (EXIF), verkleinern, als JPEG."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((size, size))
    if max(img.size) < 500:   # eBay verlangt mind. 500 px an der längsten Seite
        f = 500 / max(img.size)
        img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def create(files: list[bytes], note: str = "") -> int:
    files = [f for f in files if f][:MAX_PHOTOS]
    if not files:
        raise ValueError("Bitte mindestens ein Foto aufnehmen.")
    now = db.now_iso()
    with db.connect() as con:
        hid = con.execute("INSERT INTO handy_items(note, created_at, updated_at, step) VALUES (?, ?, ?, ?)",
                          (note.strip(), now, now, "Fotos werden gespeichert …")).lastrowid
    (PHOTO_DIR / str(hid)).mkdir(parents=True, exist_ok=True)
    for n, raw in enumerate(files, 1):
        photo_path(hid, n).write_bytes(_prepare(raw, 1600))
    _set(hid, photos=len(files))
    start_identify(hid)
    return hid


def _photos(hid: int, h: dict | None = None) -> list[bytes]:
    h = h or get(hid)
    return [photo_path(hid, n).read_bytes() for n in range(1, h["photos"] + 1)]


# Schritt 1: Was ist das? ──────────────────────────────────────────────

def start_identify(hid: int) -> None:
    _set(hid, status="erkennen", error=None, step="Claude erkennt den Artikel …")
    threading.Thread(target=identify, args=(hid,), daemon=True).start()


def identify(hid: int) -> None:
    try:
        photos = _photos(hid)
        prods = wawi.products() if wawi.available() else {}
        stock = [{"produktnr": p["produktnr"], "artikel": p["artikel"], "zustand": p["zustand"]}
                 for p in prods.values() if p["status"] == "Im Lager"]
        ident = ai.identify_item([_prepare(b, 1280) for b in photos], stock, platforms())
        if not ident["erkannt"]:
            raise ValueError("Claude konnte auf den Fotos keinen Artikel sicher erkennen – bitte deutlichere Fotos "
                             "machen (Vorderseite, Rückseite mit Barcode).")
        pnr = ident["wawi_produktnr"] if ident["wawi_produktnr"] in prods else ""
        data = {"ident": ident, "wawi_pnr": pnr, "wawi_sure": ident["wawi_sicherheit"] if pnr else "keine",
                "wawi_candidates": _candidates(ident, prods, pnr)}
        _set(hid, status="bestaetigen", step=None, data=data)
        _start_upload(hid, photos)   # Fotos schon hochladen, während du bestätigst
    except Exception as exc:
        log.exception("Handy-Erkennung %s fehlgeschlagen", hid)
        _set(hid, status="fehler", step=None, error=str(exc)[:600])


def platforms() -> list[str]:
    try:
        return ebay_account.category_aspects(VIDEOGAME_CAT).get("Plattform", {}).get("values", [])
    except Exception:
        return list(bundles.PLATFORM_SHORT)


_uploads: dict[int, threading.Thread] = {}


def _start_upload(hid: int, photos: list[bytes]) -> None:
    def run():
        try:
            urls = [ebay_trading.upload_picture(b, f"handy-{hid}-{n}.jpg") for n, b in enumerate(photos, 1)]
            _set(hid, pictures=json.dumps(urls))
        except Exception:
            log.exception("Foto-Upload %s fehlgeschlagen – wird beim Erstellen wiederholt", hid)
    t = threading.Thread(target=run, daemon=True)
    _uploads[hid] = t
    t.start()


def confirm(hid: int, form: dict) -> None:
    """Erkennung bestätigt/korrigiert + Zustandsnotiz → Schritt 2 startet."""
    h = get(hid)
    if h["status"] not in ("bestaetigen", "bereit", "fehler"):
        raise ValueError("Dieser Artikel wird gerade bearbeitet.")
    d = h["data"]
    i = d["ident"]
    old = (i["name"], i["plattform"])
    for key in ("name", "plattform", "edition", "sprache", "ean"):
        if key in form:
            i[key] = str(form[key]).strip()
    if form.get("usk") in ("", "0", "6", "12", "16", "18", "keine"):
        i["usk"] = form["usk"]
    if form.get("region") in ("PAL", "NTSC-U/C (US/Canada)", "NTSC-J (Japan)", "unbekannt"):
        i["region"] = form["region"]
    if form.get("artikel_typ") in ("videospiel", "konsole", "zubehoer", "film_musik", "buch", "sonstiges"):
        i["artikel_typ"] = form["artikel_typ"]
    if (i["name"], i["plattform"]) != old:
        i["suchbegriff"] = f"{i['name']} {_platform_short(i) or i['plattform']}".strip()
    if not i["name"]:
        raise ValueError("Bitte den Artikelnamen eintragen.")
    if "wawi_pnr" in form:
        d["wawi_pnr"] = form["wawi_pnr"] or ""
    d["confirmed"] = True
    for k in ("price_choice", "custom_price"):   # neue Analyse → neuer Preisvorschlag
        d.pop(k, None)
    _set(hid, note=str(form.get("notiz", h["note"]) or "").strip(), data=d)
    start_analyse(hid)


def back_to_confirm(hid: int) -> None:
    h = get(hid)
    if h["status"] in ("bereit", "fehler") and h["data"].get("ident"):
        _set(hid, status="bestaetigen", error=None)


def restart(hid: int) -> None:
    """Nach einem Fehler: an der richtigen Stelle neu anfangen."""
    h = get(hid)
    if h["data"].get("confirmed"):
        start_analyse(hid)
    else:
        start_identify(hid)


def start_analyse(hid: int) -> None:
    _set(hid, status="analyse", error=None, step="Claude sieht sich den Zustand an …")
    threading.Thread(target=analyse, args=(hid,), daemon=True).start()


def discard(hid: int) -> None:
    h = get(hid)
    if h and h["status"] in ("bereit", "fehler", "bestaetigen"):
        _set(hid, status="verworfen")


# ── Analyse ─────────────────────────────────────────────────────────────


def _category(ident: dict) -> tuple[str, str]:
    prefer = {"videospiel": VIDEOGAME_CAT, "konsole": CONSOLE_CAT}.get(ident["artikel_typ"])
    try:
        sugg = ebay_account.category_suggestions(ident["suchbegriff"] or ident["name"])
    except Exception:
        sugg = []
    if prefer:
        return next(((i, n) for i, n in sugg if i == prefer), (prefer, "Videospiele" if prefer == VIDEOGAME_CAT else "Konsolen"))
    if not sugg:
        raise ValueError("Keine passende eBay-Kategorie gefunden.")
    return sugg[0]


def _condition(level: str, allowed: list[dict]) -> dict:
    """Passenden erlaubten eBay-Zustand suchen – sonst die nächstschlechtere Stufe (nie besser als erkannt)."""
    if not allowed:
        return {"id": "3000", "name": "Gebraucht"}
    by_name = {c["name"].lower(): c for c in allowed}
    for lvl in CONDITION_ORDER[CONDITION_ORDER.index(level):]:
        name = CONDITION_NAMES[lvl]
        if name in by_name:
            return by_name[name]
        hit = next((c for c in allowed if name in c["name"].lower() and not (name == "gut" and "sehr" in c["name"].lower())), None)
        if hit:
            return hit
    return next((c for c in allowed if c["name"].lower() == "gebraucht"), allowed[-1])


def _completeness(umfang: dict) -> str | None:
    if all(umfang[k] == "ja" for k in ("huelle", "anleitung", "datentraeger")):
        return "cib"
    if umfang["datentraeger"] == "ja" and "nein" in (umfang["huelle"], umfang["anleitung"]):
        return "teil"
    return None


def _platform_short(ident: dict) -> str | None:
    return bundles.PLATFORM_SHORT.get(ident["plattform"]) or market._title_platform(ident["plattform"] or "")[0]


def _market(ident: dict, category_id: str, condition_id: str, ref_name: str | None) -> dict:
    plat = _platform_short(ident)
    name = ident["name"].strip()
    if ident["artikel_typ"] == "videospiel" and plat and name:
        query = f"{name} {plat}"
        must = [w for w in market._norm(name).split() if (len(w) > 1 or w.isdigit()) and w not in ("the", "of", "und", "and")]
        plat_tokens = market.PLATFORM_TOKENS.get(plat, [])
    else:
        query = ident["suchbegriff"] or name
        must = [w for w in market._norm(query).split() if len(w) > 2]
        plat_tokens = []
    ean = ident["ean"] if _valid_gtin(ident["ean"]) else None
    offers = market.search(query, must, plat_tokens, category_id, ean, new=condition_id in ("1000", "1500"))
    comp = [o for o in offers if market._comparable(o, _completeness(ident["umfang"]), market.COND_RANK_ID.get(condition_id))]
    median, quick = market.price_levels([o["total"] for o in comp])
    sales = market.own_sales(None, query, ref_name=ref_name)
    return {
        "query": query, "rough": not plat_tokens, "found": len(comp), "excluded": len(offers) - len(comp),
        "median": round(median, 2) if median else None, "quick": round(quick, 2) if quick else None,
        "samples": comp[:8], "own_sales": sales,
        "sold_avg": round(statistics.mean(s["price"] for s in sales), 2) if sales else None,
        "sold_url": market.sold_search_url(query, category_id),
    }


def _valid_gtin(code: str | None) -> bool:
    if not code or not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


_defaults_cache: dict = {}


def _defaults() -> dict:
    """Artikelstandort und Rücknahme-/Zahlungsrichtlinie wie bei deinen bisherigen Angeboten."""
    if not _defaults_cache:
        with db.connect() as con:
            r = con.execute("SELECT item_id FROM listings WHERE active = 1 AND listing_type = 'FixedPriceItem' "
                            "ORDER BY start_time DESC LIMIT 1").fetchone()
        d = ebay_trading.item_details(r["item_id"]) if r else {}
        _defaults_cache.update({
            "location": d.get("location"), "postal_code": d.get("postal_code"), "country": d.get("country") or "DE",
            "return_profile": d.get("return_profile") or ebay_account.return_profiles()[0]["id"],
            "payment_profile": d.get("payment_profile") or ebay_account.payment_profiles()[0]["id"],
        })
    return dict(_defaults_cache)


def analyse(hid: int) -> None:
    """Schritt 2: Zustand (Fotos + Notiz), Kategorie, Marktpreise, Text, eBay-Prüfung."""
    try:
        h = get(hid)
        d = h["data"]
        ident = d["ident"]
        photos = _photos(hid, h)
        facts = {k: v for k, v in ident.items()
                 if k not in ("wawi_produktnr", "wawi_sicherheit", "unsicherheiten", "suchbegriff", "erkannt")}
        cond_info = ai.assess_condition([_prepare(b, 1280) for b in photos], h["note"], facts)
        ident.update(umfang=cond_info["umfang"], zustand=cond_info["zustand"], zustand_details=cond_info["zustand_details"])
        d["doubts"] = cond_info["unsicherheiten"]

        _set(hid, step="Kategorie & Marktpreise …")
        prods = wawi.products() if wawi.available() else {}
        pnr = d.get("wawi_pnr") if d.get("wawi_pnr") in prods else ""
        cat_id, cat_name = _category(ident)
        conds = ebay_account.conditions(cat_id)
        cond = _condition(ident["zustand"], conds)
        d.update({
            "category_id": cat_id, "category_name": cat_name, "conditions": conds, "condition_id": cond["id"],
            "wawi_pnr": pnr, "wawi_candidates": _candidates(ident, prods, pnr),
            "ean": ident["ean"] if _valid_gtin(ident["ean"]) else None,
            "market": _market(ident, cat_id, cond["id"], prods[pnr]["artikel"] if pnr else None),
            **_defaults(),
        })

        _set(hid, step="Claude schreibt Titel & Beschreibung …")
        aspects = ebay_account.category_aspects(cat_id)
        facts.update(umfang=ident["umfang"], zustand=ident["zustand"], zustand_details=ident["zustand_details"])
        text = ai.write_single_text(facts, h["note"], cond["name"], aspects, d["doubts"])
        specifics = text["specifics"]
        if "Plattform" in aspects and ident["plattform"]:
            specifics["Plattform"] = [ident["plattform"]]      # vom Händler bestätigt
        if "Spielname" in aspects and ident["name"] and "Spielname" not in specifics:
            specifics["Spielname"] = [ident["name"]]
        if "USK-Einstufung" in aspects:
            specifics.pop("USK-Einstufung", None)
            if ident["usk"] not in ("", "keine"):
                specifics["USK-Einstufung"] = [f"USK ab {ident['usk']} Jahren"]
        d.update(title=text["title"], body=text["description"], specifics=specifics, text_by="claude")

        _set(hid, step="Fotos zu eBay hochladen …")
        t = _uploads.pop(hid, None)
        if t:
            t.join(180)
        pics = json.loads(get(hid).get("pictures") or "[]")
        if len(pics) != len(photos):
            pics = [ebay_trading.upload_picture(b, f"handy-{hid}-{n}.jpg") for n, b in enumerate(photos, 1)]
            _set(hid, pictures=json.dumps(pics))
        d["pictures"] = pics

        _set(hid, step="Preis, Versand & Gewinn berechnen …")
        recalc(d)
        _set(hid, step="eBay prüft das Angebot …")
        verify(d)
        _set(hid, status="bereit", step=None, data=d)
    except Exception as exc:
        log.exception("Handy-Analyse %s fehlgeschlagen", hid)
        _set(hid, status="fehler", step=None, error=str(exc)[:600])


def _candidates(ident: dict, prods: dict, pnr: str) -> list[dict]:
    """Auswahl für die WaWi-Zuordnung: Claudes Treffer + ähnliche Artikel im Lager."""
    probe = f"{ident['name']} {ident['plattform']} {_platform_short(ident) or ''}"
    pool = [p for p in prods.values() if p["status"] == "Im Lager"]
    scored = sorted(pool, key=lambda p: -wawi._score(probe, p["artikel"]))[:8]
    if pnr and pnr not in {p["produktnr"] for p in scored}:
        scored.insert(0, prods[pnr])
    return [{"produktnr": p["produktnr"], "artikel": p["artikel"], "zustand": p["zustand"],
             "ek": p["ek"], "min_vk": p["min_vk"]} for p in scored]


# ── Preis, Versand, Steuer ──────────────────────────────────────────────

def _grid_down(v: float) -> float:
    """Auf ,49 bzw. ,99 abrunden (mind. 0,99)."""
    return max(0.99, math.floor((v + 0.01) * 2) / 2 - 0.01)


def shipping_for(article: float, usk18: bool) -> dict:
    """Versand nach deiner Regel: Warenwert = Artikelpreis (1 Spiel ≤ 10 € Brief, … Ü18 immer „Alter“ KP)."""
    own, kind = wawi.porto_rule(1, article, usk18)
    prof = bundles.pick_profile(1, article, usk18) or {}
    return {"own": own, "kind": kind, "profile": prof.get("id"), "profile_name": prof.get("name", "?"),
            "buyer": prof.get("buyer_cost", 0.0), "age_check": bool(prof.get("age_check"))}


def _profit(article: float, ship: dict, w: dict | None, tax: str | None) -> dict | None:
    if not w:
        return None
    w2 = {**w, "versand_kaeufer": ship["buyer"]}
    if tax:
        w2["tax"] = tax
    return wawi.item_profit(article, w2, ship["own"])


def _article_for_total(total: float, usk18: bool) -> float:
    """Höchster ,49/,99-Artikelpreis, bei dem Artikel + Käuferversand den Zielgesamtpreis nicht übersteigt."""
    a = _grid_down(total)
    while a > 0.99:
        if a + shipping_for(a, usk18)["buyer"] <= total + 0.001:
            return a
        a = round(a - 0.5, 2)
    return 0.99


def _min_article(w: dict | None, usk18: bool, tax: str | None) -> float:
    """Kleinster ,49/,99-Preis mit mindestens dem Ziel-Gewinn je Artikel."""
    if not w:
        return 0.99
    target = settings.get("min_profit_per_item")
    a = 0.99
    while a < 2000:
        if _profit(a, shipping_for(a, usk18), w, tax)["profit"] + 1e-6 >= target:
            return a
        a = round(a + 0.5, 2)
    return a


def _tax_mode(w: dict | None) -> str | None:
    label = (w or {}).get("tax_label", "")
    if "25a" in label:
        return "25a"
    if "regel" in label.lower():
        return "regel"
    return None   # ungeklärt / nicht in der WaWi → du wählst in der Vorschau


def recalc(data: dict) -> None:
    """Preisvorschläge, Versand, Gewinn und Hinweise neu berechnen (nach jeder Änderung)."""
    prods = wawi.products() if wawi.available() else {}
    w = prods.get(data.get("wawi_pnr") or "")
    data["wawi"] = ({k: w[k] for k in ("produktnr", "artikel", "status", "zustand", "ek", "min_vk", "tax_label", "lager_reihe")}
                    if w else None)
    data["tax_mode"] = data.get("tax_override") or _tax_mode(w)
    tax = data["tax_mode"]
    ident = data["ident"]
    # Ü18-Versand: USK 18 – und auch Ware OHNE USK-Kennzeichen (Import, nur PEGI/ESRB), § 12 Abs. 3 JuSchG
    usk18 = ident["usk"] in ("18", "keine") or bool(wawi.USK18_RE.search(f"{data.get('title', '')} {(w or {}).get('artikel', '')}"))
    data["usk18"] = usk18
    min_a = _min_article(w, usk18, tax)
    data["min_article"] = min_a if w else None

    m = data["market"]
    options, seen = [], set()
    for key, label, total in (("schnell", "Schnell verkaufen", m.get("quick")),
                              ("markt", "Marktpreis", m.get("median")),
                              ("bewaehrt", "Wie zuletzt verkauft", m.get("sold_avg"))):
        if not total:
            continue
        a = _article_for_total(total, usk18)
        raised = w is not None and a < min_a
        a = max(a, min_a)
        if a in seen:
            continue
        seen.add(a)
        options.append({"key": key, "label": label, "article": a, "raised": raised, "ref_total": total})
    if not options:
        options.append({"key": "mindest", "label": "Mindestpreis (kein Marktvergleich)", "article": min_a if w else 9.99,
                        "raised": False, "ref_total": None})
    for o in options:
        o["ship"] = shipping_for(o["article"], usk18)
        o["total"] = round(o["article"] + o["ship"]["buyer"], 2)
        o["profit"] = _profit(o["article"], o["ship"], w, tax)
    data["options"] = options
    keys = [o["key"] for o in options]
    data["recommended"] = next(k for k in ("markt", "bewaehrt", "schnell", "mindest") if k in keys)

    choice = data.get("price_choice") or data["recommended"]
    if choice == "eigen" and data.get("custom_price"):
        price = round(float(data["custom_price"]), 2)
    else:
        opt = next((o for o in options if o["key"] == choice), None) or next(o for o in options if o["key"] == data["recommended"])
        choice, price = opt["key"], opt["article"]
    data["price_choice"], data["price"] = choice, price
    ship = shipping_for(price, usk18)
    data["shipping"] = ship
    data["total"] = round(price + ship["buyer"], 2)
    data["profit"] = _profit(price, ship, w, tax)
    data["below_min"] = bool(w) and price < min_a
    data["vat_percent"] = 19 if tax == "regel" else None

    hints = []
    if tax == "25a":
        hints.append(TAX_NOTE_25A)
    if usk18:
        hints.append(NO_USK_NOTE if ident["usk"] == "keine" else AGE_NOTE)
    data["hints"] = hints
    data["description"] = data["body"] + ("<h3>Hinweise</h3><ul>" + "".join(f"<li>{h}</li>" for h in hints) + "</ul>" if hints else "")


def listing_data(data: dict) -> dict:
    return {
        "title": data["title"], "description": data["description"], "category_id": data["category_id"],
        "price": data["price"], "condition_id": data["condition_id"], "country": data.get("country") or "DE",
        "location": data.get("location"), "postal_code": data.get("postal_code"),
        "sku": data.get("wawi_pnr") or None, "pictures": data["pictures"], "specifics": data["specifics"],
        "shipping_profile": data["shipping"]["profile"], "return_profile": data["return_profile"],
        "payment_profile": data["payment_profile"], "ean": data.get("ean"), "vat_percent": data.get("vat_percent"),
    }


def verify(data: dict) -> None:
    try:
        res = ebay_trading.verify_listing(listing_data(data))
        data["verify"] = {"ok": True, "fees": res["fees"], "fee_sum": ebay_trading.fee_total(res["fees"]),
                          "warnings": res["warnings"]}
    except Exception as exc:
        data["verify"] = {"ok": False, "error": str(exc)[:500]}


def storage_rows() -> list[dict]:
    try:
        usage = wawi.storage_usage()
    except Exception:
        usage = {}
    return [{"name": f"Reihe {i}", "used": usage.get(f"Reihe {i}", 0)} for i in range(1, 17)]


# ── Ändern & Einstellen ─────────────────────────────────────────────────

def update(hid: int, form: dict) -> None:
    h = get(hid)
    if h["status"] != "bereit":
        raise ValueError("Dieser Artikel kann nicht mehr geändert werden.")
    d = h["data"]
    d["title"] = (form.get("title") or d["title"]).strip()[:80]
    if form.get("body"):
        d["body"] = form["body"]
    if form.get("condition_id") and any(c["id"] == form["condition_id"] for c in d["conditions"]):
        d["condition_id"] = form["condition_id"]
    if "wawi_pnr" in form:
        d["wawi_pnr"] = form["wawi_pnr"] or ""
    d["tax_override"] = form.get("tax") or None
    d["price_choice"] = form.get("price_choice") or d.get("price_choice")
    custom = (form.get("custom_price") or "").replace(",", ".").strip()
    if d["price_choice"] == "eigen":
        if not custom:
            raise ValueError("Bitte einen eigenen Preis eintragen.")
        d["custom_price"] = max(0.99, round(float(custom), 2))
    d["lager_reihe"] = form.get("lager_reihe") or None
    recalc(d)
    verify(d)
    _set(hid, data=d)


def publish(hid: int, force: bool = False) -> dict:
    with db.connect() as con:
        claimed = con.execute("UPDATE handy_items SET status = 'einstellen' WHERE id = ? AND status = 'bereit'",
                              (hid,)).rowcount
    if not claimed:
        raise ValueError("Dieser Artikel ist nicht (mehr) bereit zum Einstellen.")
    try:
        d = get(hid)["data"]
        recalc(d)   # frische WaWi-Daten
        if not d["tax_mode"]:
            raise ValueError("Bitte die Steuerart wählen (§ 25a oder Regelbesteuerung) – in der WaWi ist sie nicht festgelegt.")
        if d["wawi"] and d["wawi"]["status"] != "Im Lager":
            raise ValueError(f"Der WaWi-Artikel {d['wawi_pnr']} ist nicht mehr „Im Lager“ (Status: {d['wawi']['status']}).")
        if d["usk18"] and not d["shipping"]["age_check"]:
            raise ValueError("USK-18-Spiel, aber kein Versandprofil mit Altersprüfung gefunden.")
        if d["below_min"] and not force:
            raise ValueError(f"Preis liegt unter deinem Mindestpreis ({d['min_article']:.2f} €) – "
                             "Preis anheben oder „Trotzdem einstellen“ anhaken.".replace(".", ","))
        res = ebay_trading.add_listing(listing_data(d))
    except Exception:
        _set(hid, status="bereit")
        raise
    item_id = res["item_id"]
    problems = []
    if d.get("wawi_pnr"):
        wawi.link(item_id, d["wawi_pnr"])
        try:
            wawi.set_listed(d["wawi_pnr"], d.get("lager_reihe"))
        except Exception as exc:
            problems.append(f"WaWi-Status nicht gesetzt: {exc}")
    try:
        from . import meta
        meta.save(meta.from_details(ebay_trading.item_details(item_id)))
    except Exception:
        log.exception("Steckbrief für %s nicht gespeichert", item_id)
    d["fees"], d["problems"] = res["fees"], problems
    _set(hid, status="online", item_id=item_id, data=d)
    return {"item_id": item_id, "problems": problems}


def item_url(item_id: str) -> str:
    return f"https://www.ebay.de/itm/{item_id}"

