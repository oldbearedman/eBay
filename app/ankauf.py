"""Ankauf erfassen: Konvolut fotografieren → Claude listet alle Artikel → Marktwerte → EK-Aufteilung nach
Marktwert (bei § 25a steuerlich günstig und nachvollziehbar) → ein Ankaufbeleg mit allen Artikeln in der WaWi."""
import json
import logging
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from . import ai, config, db, ebay_trading, handy, market, wawi

log = logging.getLogger("ebay-manager")

PHOTO_DIR = config.DATA_DIR / "ankauf"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
MAX_PHOTOS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS ankauf (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'erkennen',  -- erkennen | bewerten | pruefen | anlegen | angelegt | fehler | verworfen
    step        TEXT,
    hint        TEXT NOT NULL DEFAULT '',
    photos      INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL DEFAULT '{}',
    belegnr     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

UMFANG = ["komplett", "ohne Anleitung", "nur Disc/Modul", "Hülle ohne Disc", "versiegelt", "Gerät", "Sonstiges"]
ZUSTAND = ["Neu", "Gebraucht - Hervorragend", "Gebraucht - Gut", "Gebraucht - Akzeptabel", "Gebraucht", "Defekt / Ersatzteile"]
ART = {"videospiel": "Videospiele", "konsole": "Konsolen", "zubehoer": "Zubehör", "film_musik": "Medien",
       "buch": "Medien", "sonstiges": "Sonstiges"}
# Kurzform im WaWi-Artikelnamen – wie bisher bei dir („… PS2 CIB“, „… ohne B.“)
UMFANG_SUFFIX = {"komplett": "CIB", "ohne Anleitung": "ohne B.", "nur Disc/Modul": "nur Disc",
                 "Hülle ohne Disc": "Hülle + Anleitung ohne Disc", "versiegelt": "OVP sealed", "Gerät": "", "Sonstiges": ""}
COMPLETENESS = {"komplett": "cib", "versiegelt": "cib", "ohne Anleitung": "teil", "nur Disc/Modul": "teil"}
COND_RANK = {"Neu": 0, "Gebraucht - Hervorragend": 1, "Gebraucht - Gut": 3, "Gebraucht": 3,
             "Gebraucht - Akzeptabel": 4, "Defekt / Ersatzteile": 6}
# Schätzwerte, wenn es keine Vergleichsangebote gibt (werden als „geschätzt“ markiert und sind änderbar)
FALLBACK = {"Hülle ohne Disc": 1.00, "nur Disc/Modul": 2.00, "Sonstiges": 2.00}
DEFAULT_VALUE = 3.00

QUELLEN = ["eBay", "Kleinanzeigen", "Privatkauf", "Flohmarkt", "Privatbestand", "Sonstiges"]
ZAHLUNG = ["Überweisung", "PayPal", "Bar", "Karte", "Sonstiges"]
UST_HERKUNFT = ["Privatperson", "Kleinunternehmer nach § 19 UStG", "Wiederverkäufer mit Differenzbesteuerung",
                "Unternehmer aus nichtunternehmerischem Bereich", "Unternehmer mit normalem Umsatzsteuerausweis"]


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)
        con.execute("UPDATE ankauf SET status = 'fehler', error = 'Unterbrochen (Neustart) – bitte neu starten.' "
                    "WHERE status IN ('erkennen', 'bewerten')")
        con.execute("UPDATE ankauf SET status = 'pruefen' WHERE status = 'anlegen' AND belegnr IS NULL")


# ── Speicher ────────────────────────────────────────────────────────────

def _set(aid: int, **fields) -> None:
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    fields["updated_at"] = db.now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as con:
        con.execute(f"UPDATE ankauf SET {cols} WHERE id = ?", (*fields.values(), aid))


def get(aid: int) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM ankauf WHERE id = ?", (aid,)).fetchone()
    return {**dict(r), "data": json.loads(r["data"])} if r else None


def recent(limit: int = 20) -> list[dict]:
    with db.connect() as con:
        rows = con.execute("SELECT id FROM ankauf WHERE status != 'verworfen' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [get(r["id"]) for r in rows]


def photo_path(aid: int, n: int):
    return PHOTO_DIR / str(aid) / f"{n}.jpg"


def create(files: list[bytes], hint: str = "", ebay_item: str = "") -> int:
    files = [f for f in files if f][:MAX_PHOTOS]
    if not files:
        raise ValueError("Bitte mindestens ein Foto vom Konvolut machen.")
    now = db.now_iso()
    with db.connect() as con:
        aid = con.execute("INSERT INTO ankauf(hint, created_at, updated_at) VALUES (?, ?, ?)",
                          (hint.strip(), now, now)).lastrowid
    (PHOTO_DIR / str(aid)).mkdir(parents=True, exist_ok=True)
    for n, raw in enumerate(files, 1):
        photo_path(aid, n).write_bytes(handy._prepare(raw, 1600))
    data = {"kauf": _kauf_defaults(ebay_item)}
    _set(aid, photos=len(files), data=data)
    start(aid)
    return aid


def start(aid: int) -> None:
    _set(aid, status="erkennen", error=None, step="Claude sieht sich das Konvolut an …")
    threading.Thread(target=_run, args=(aid,), daemon=True).start()


def discard(aid: int) -> None:
    a = get(aid)
    if a and a["status"] in ("pruefen", "fehler"):
        _set(aid, status="verworfen")


# ── eBay-Käufe, die noch nicht in der WaWi sind ─────────────────────────

def open_purchases() -> list[dict]:
    try:
        known = wawi.known_ebay_items()
        with db.connect() as con:
            used = {json.loads(r["data"]).get("kauf", {}).get("ebay_artikelnr")
                    for r in con.execute("SELECT data FROM ankauf WHERE status NOT IN ('verworfen', 'fehler')")}
        return [p for p in ebay_trading.purchases(60) if p["item_id"] not in known and p["item_id"] not in used]
    except Exception:
        log.exception("eBay-Käufe nicht abrufbar")
        return []


def _de_date(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%y")
    except ValueError:
        return iso


def _kauf_defaults(ebay_item: str = "") -> dict:
    k = {"quelle": "Privatkauf", "kaufdatum": date.today().strftime("%d.%m.%y"), "verkaeufer": "", "adresse": "",
         "zahlungsart": "PayPal", "gesamt": "", "ust_herkunft": "Privatperson", "rechnung": "Nein",
         "ebay_verkaeufer": "", "ebay_artikelnr": "", "ebay_bestellnr": "", "eigenbeleg": "",
         "verteilung": "Nach Vergleichs-/Marktwert"}
    if ebay_item:
        p = next((p for p in ebay_trading.purchases(60) if p["item_id"] == ebay_item), None)
        if p:
            k.update(quelle="eBay", kaufdatum=_de_date(p["date"]), verkaeufer=f"eBay Privatverkäufer {p['seller']}",
                     adresse="nicht bekannt (eBay-Kauf)", zahlungsart="Karte", gesamt=f"{p['total']:.2f}".replace(".", ","),
                     ebay_verkaeufer=p["seller"], ebay_artikelnr=p["item_id"], ebay_titel=p["title"],
                     ebay_preis=p["price"], ebay_versand=p["shipping"])
    return k


# ── Erkennen & Bewerten ─────────────────────────────────────────────────

def _run(aid: int) -> None:
    try:
        a = get(aid)
        photos = [handy._prepare(photo_path(aid, n).read_bytes(), 1280) for n in range(1, a["photos"] + 1)]
        res = ai.identify_lot(photos, a["hint"])
        items = []
        for it in res["artikel"]:
            items.append({**it, "anzahl": max(1, int(it["anzahl"] or 1)), "wert": None, "quelle": None, "wert_manuell": None})
        if not items:
            raise ValueError("Claude hat auf den Fotos keine Artikel gefunden – bitte deutlichere Fotos machen.")
        d = a["data"]
        d.update(items=items, doubts=res["unsicherheiten"])
        _set(aid, data=d)
        _value(aid)
    except Exception as exc:
        log.exception("Ankauf %s: Erkennung fehlgeschlagen", aid)
        _set(aid, status="fehler", step=None, error=str(exc)[:600])


def _key(it: dict) -> tuple:
    return (it["name"].strip().lower(), it["plattform"].strip().lower(), it["umfang"], it["zustand"])


def _market_value(it: dict) -> tuple[float | None, str, str]:
    """(Vergleichswert, Quelle, Link zu den verkauften Angeboten) – eigene Verkäufe vor aktuellen Angeboten."""
    name, plat = it["name"].strip(), it["plattform"].strip()
    query = f"{name} {plat}".strip()
    sold_url = market.sold_search_url(query)
    if it["umfang"] == "Hülle ohne Disc" or name.lower().startswith("unbekannt"):
        return None, "", sold_url
    sales = market.own_sales(None, query)
    if sales:
        return round(statistics.mean(s["price"] for s in sales), 2), f"Ø {len(sales)} eigene Verkäufe", sold_url
    must = [w for w in market._norm(name).split() if (len(w) > 1 or w.isdigit()) and w not in ("the", "of", "und", "and")]
    plat_tokens = market.PLATFORM_TOKENS.get(plat, [])
    try:
        offers = market.search(query, must, plat_tokens, None, None, new=it["umfang"] == "versiegelt")
    except Exception:
        return None, "", sold_url
    comp = [o for o in offers if market._comparable(o, COMPLETENESS.get(it["umfang"]), COND_RANK.get(it["zustand"]))]
    _, quick = market.price_levels([o["total"] for o in comp])
    if quick:
        src = f"unteres Drittel von {len(comp)} eBay-Angeboten" if len(comp) >= 3 else             f"nur {len(comp)} Vergleichsangebot{'e' if len(comp) > 1 else ''} – prüfen"
        return round(quick, 2), src, sold_url
    return None, "", sold_url


def _value(aid: int, only_missing: bool = False) -> None:
    """Vergleichswerte für alle (bzw. neue/geänderte) Titel ermitteln."""
    _set(aid, status="bewerten", step="Marktwerte werden ermittelt …")
    try:
        d = get(aid)["data"]
        items = d["items"]
        todo = {}
        for it in items:
            if not only_missing or it.get("quelle") is None:
                todo.setdefault(_key(it), it)
        with ThreadPoolExecutor(4) as pool:
            results = dict(zip(todo, pool.map(_market_value, todo.values())))
        for it in items:
            if _key(it) in results:
                v, src, url = results[_key(it)]
                if v is None:
                    v, src = FALLBACK.get(it["umfang"], DEFAULT_VALUE), "geschätzt"
                it.update(wert=v, quelle=src, sold_url=url)
        d["valued_at"] = date.today().strftime("%d.%m.%Y")
        recalc(d)
        _set(aid, status="pruefen", step=None, data=d)
    except Exception as exc:
        log.exception("Ankauf %s: Bewertung fehlgeschlagen", aid)
        _set(aid, status="fehler", step=None, error=str(exc)[:600])


# ── Aufteilung ──────────────────────────────────────────────────────────

def _money(v) -> float:
    return wawi.money(v) if v not in (None, "") else 0.0


def recalc(d: dict) -> None:
    """EK je Exemplar: Gesamtpreis × (Wert / Summe aller Werte) – bzw. gleichmäßig. Cent-Rest auf das teuerste."""
    k = d["kauf"]
    total = _money(k.get("gesamt"))
    units = [(i, it) for i, it in enumerate(d["items"]) for _ in range(it["anzahl"])]
    values = [(it["wert_manuell"] if it.get("wert_manuell") is not None else it.get("wert") or DEFAULT_VALUE) for _, it in units]
    sum_values = round(sum(values), 2)
    n = len(units)
    if k["verteilung"] == "Gleichmäßig verteilt":
        shares = [1 / n] * n if n else []
    else:
        shares = [v / sum_values for v in values] if sum_values else [1 / n] * n
    eks = [round(total * s, 2) for s in shares]
    if eks:
        eks[max(range(n), key=lambda j: values[j])] += round(total - sum(eks), 2)
    per_item = {}
    for (i, _), v, ek in zip(units, values, eks):
        per_item.setdefault(i, []).append((v, round(ek, 2)))
    for i, it in enumerate(d["items"]):
        it["ek_je"] = [ek for _, ek in per_item.get(i, [])]
        it["wert_eff"] = per_item.get(i, [(None, 0)])[0][0]
    d["summe"] = {"artikel": n, "werte": sum_values, "gesamt": round(total, 2), "ek_summe": round(sum(eks), 2),
                  "anteil": round(total / sum_values * 100, 2) if sum_values else None,
                  "geschaetzt": sum(1 for _, it in units if it.get("quelle") == "geschätzt" and it.get("wert_manuell") is None)}


def schaetzgrundlage(d: dict) -> str:
    s = d["summe"]
    if d["kauf"]["verteilung"] == "Gleichmäßig verteilt":
        return f"Konvolut {s['artikel']} Artikel; Gesamtpreis gleichmäßig auf alle Artikel verteilt"
    sources = {it["quelle"] for it in d["items"] if it.get("quelle")}
    parts = [f"Konvolut {s['artikel']} Artikel. Aufteilung nach Marktwert: Vergleichswert je Titel = Ø eigene Verkäufe"
             " (falls vorhanden), sonst unteres Drittel vergleichbarer aktueller eBay-Festpreisangebote (DE, gleicher"
             f" Umfang/Zustand, inkl. Versand), Stand {d.get('valued_at', '')}"]
    manual = sum(1 for it in d["items"] if it.get("wert_manuell") is not None)
    if s["geschaetzt"] or manual or "geschätzt" in sources:
        parts.append(f"{s['geschaetzt']} Werte pauschal geschätzt (ohne Vergleichsangebote), {manual} manuell angepasst")
    parts.append(f"Marktwert gesamt {s['werte']:.2f} EUR, Anteil {s['anteil']:.2f} %".replace(".", ","))
    return "; ".join(parts)


def eigenbeleg_text(k: dict) -> str:
    total = k.get("gesamt") or "?"
    if k["quelle"] == "eBay":
        return (f"Kein Beleg vom Verkäufer; Nachweis eBay-Kauf Artikel {k.get('ebay_artikelnr')}"
                + (f" / Bestellung {k['ebay_bestellnr']}" if k.get("ebay_bestellnr") else "")
                + f" vom {k['kaufdatum']}: {total} € per {k['zahlungsart']}")
    if k["quelle"] == "Privatbestand":
        return f"Übernahme aus Privatbestand am {k['kaufdatum']}, vereinbarter Übernahmepreis {total} €"
    return f"Kein Beleg vom Privatverkäufer; Zahlung {total} € per {k['zahlungsart']} am {k['kaufdatum']}"


def artikel_name(it: dict) -> str:
    parts = [it["name"].strip(), it["plattform"].strip(), UMFANG_SUFFIX.get(it["umfang"], "")]
    name = " ".join(p for p in parts if p)
    if it.get("fremdfassung"):
        name += " (PEGI/Import)"
    return name[:200]   # Claudes Randnotiz (it["hinweis"]) bleibt in der Prüfansicht – kurze Namen ordnen sich besser zu


# ── Ändern & Anlegen ────────────────────────────────────────────────────

def update(aid: int, form) -> bool:
    """Formular übernehmen. True, wenn neue/geänderte Titel neu bewertet werden müssen."""
    a = get(aid)
    if a["status"] != "pruefen":
        raise ValueError("Dieser Ankauf kann gerade nicht geändert werden.")
    d = a["data"]
    items, revalue = [], False
    for i in range(int(form.get("n", 0)) + 3):
        name = (form.get(f"name_{i}") or "").strip()
        if not name or form.get(f"weg_{i}") == "on":
            continue
        old = d["items"][i] if i < len(d["items"]) else None
        it = dict(old or {"art": "videospiel", "fremdfassung": False, "hinweis": "", "wert": None, "quelle": None})
        it.update(name=name, plattform=(form.get(f"plattform_{i}") or "").strip(),
                  umfang=form.get(f"umfang_{i}") if form.get(f"umfang_{i}") in UMFANG else it.get("umfang", "komplett"),
                  zustand=form.get(f"zustand_{i}") if form.get(f"zustand_{i}") in ZUSTAND else it.get("zustand", "Gebraucht"),
                  anzahl=max(1, int(form.get(f"anzahl_{i}") or 1)),
                  fremdfassung=form.get(f"fremd_{i}") == "on")
        if form.get(f"art_{i}") in ART:
            it["art"] = form[f"art_{i}"]
        if old is None or _key(old) != _key(it):
            it.update(wert=None, quelle=None, wert_manuell=None)
            revalue = True
        else:
            # Wert-Feld zeigt den wirksamen Wert; weicht er vom ermittelten ab, gilt er als manuell gesetzt
            wm = (form.get(f"wert_{i}") or "").replace(",", ".").strip()
            try:
                v = round(float(wm), 2) if wm else None
            except ValueError:
                v = None
            auto = it.get("wert")
            it["wert_manuell"] = None if v is None or (auto is not None and abs(v - auto) < 0.005) else v
        items.append(it)
    d["items"] = items
    k = d["kauf"]
    for f in ("quelle", "kaufdatum", "verkaeufer", "adresse", "zahlungsart", "gesamt", "ust_herkunft", "rechnung",
              "ebay_verkaeufer", "ebay_artikelnr", "ebay_bestellnr", "eigenbeleg", "verteilung"):
        if f in form:
            k[f] = str(form[f]).strip()
    recalc(d)
    _set(aid, data=d)
    return revalue


def revalue(aid: int) -> None:
    threading.Thread(target=_value, args=(aid, True), daemon=True).start()


def check(d: dict) -> list[str]:
    """Pflichtangaben für den Beleg."""
    k, problems = d["kauf"], []
    if _money(k.get("gesamt")) <= 0:
        problems.append("Gesamtpreis fehlt")
    if not k.get("verkaeufer"):
        problems.append("Verkäufer fehlt")
    try:
        datetime.strptime(k.get("kaufdatum", ""), "%d.%m.%y")
    except ValueError:
        problems.append("Kaufdatum im Format TT.MM.JJ angeben")
    if k["quelle"] == "eBay" and not k.get("ebay_artikelnr"):
        problems.append("eBay-Artikelnummer fehlt")
    if not d.get("items"):
        problems.append("keine Artikel")
    if abs(d["summe"]["ek_summe"] - d["summe"]["gesamt"]) > 0.001:
        problems.append("EK-Summe passt nicht zum Gesamtpreis")
    return problems


def build_rows(d: dict) -> list[dict]:
    """Belegzeilen für die WaWi: je Exemplar eine Zeile, alle mit denselben Belegangaben."""
    recalc(d)
    problems = check(d)
    if problems:
        raise ValueError("Bitte ergänzen: " + ", ".join(problems))
    k = d["kauf"]
    eigen = k.get("eigenbeleg") or eigenbeleg_text(k)
    grund = schaetzgrundlage(d)
    n_units = d["summe"]["artikel"]
    context = {
        "kaufdatum": k["kaufdatum"], "verkaeufer": k["verkaeufer"], "verkaeufer_adresse": k.get("adresse") or "nicht bekannt",
        "quelle": k["quelle"],
        "zahlungsart": k["zahlungsart"], "zahlungsziel": k["kaufdatum"],
        "zahlungsziel_zahlungsart": "Bar" if k["zahlungsart"] == "Bar" else "Überweisung",
        "eigenbeleg_grund": eigen, "rechnung_vorhanden": k.get("rechnung") or "Nein",
        "ebay_verkaeufer": k.get("ebay_verkaeufer", ""), "ebay_bestellnr": k.get("ebay_bestellnr", ""),
        "ebay_artikelnr": k.get("ebay_artikelnr", ""), "ust_herkunft": k["ust_herkunft"],
        "ek_grundlage": "Konvolut anteilig" if n_units > 1 else "Einzelpreis",
        "verteilungsmethode": k["verteilung"] if n_units > 1 else "Einzelpreis",
        "gleichverteilung_bestaetigt": "Ja" if k["verteilung"] == "Gleichmäßig verteilt" and n_units > 1 else "Nein",
        "schaetzgrundlage": grund if n_units > 1 else "",
        "ebay_gebuehrenprofil": "Auto", "status": "Im Lager",
    }
    return [{**context, "artikel": artikel_name(it),
             "kategorie": "Zubehör" if it["umfang"] == "Hülle ohne Disc" else ART[it["art"]],
             "zustand": it["zustand"], "ek": wawi.fmt_money(ek)}
            for it in d["items"] for ek in it["ek_je"]]


def create_receipt(aid: int) -> dict:
    with db.connect() as con:
        claimed = con.execute("UPDATE ankauf SET status = 'anlegen' WHERE id = ? AND status = 'pruefen'", (aid,)).rowcount
    if not claimed:
        raise ValueError("Dieser Ankauf ist nicht (mehr) bereit.")
    try:
        d = get(aid)["data"]
        res = wawi.create_receipt(build_rows(d))
    except Exception:
        _set(aid, status="pruefen")
        raise
    d["angelegt"] = res["rows"]
    _set(aid, status="angelegt", belegnr=res["belegnr"], data=d)
    return res


def receipt_pdf(belegnr: str) -> bytes:
    import httpx
    r = httpx.get(f"{wawi.WAWI_URL}/ankaufbeleg/{belegnr}.pdf", timeout=60)
    r.raise_for_status()
    return r.content
