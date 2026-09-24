"""„Bündel vorschlagen“: Claude Opus 5.5 analysiert Bestand, Marktpreise und Bestellhistorie."""
import json
import logging
import threading
from datetime import datetime, timezone

import anthropic

from . import ai, db, ebay_orders, market, settings, traffic, wawi

log = logging.getLogger("ebay-manager")

MODEL = ai.MODEL
PRICE_IN, PRICE_OUT = 4.0, 20.0  # $ pro Million Tokens (Claude Opus 5.5)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,        -- laeuft | fertig | fehler
    result      TEXT,                 -- JSON
    error       TEXT,
    cost_usd    REAL,
    stats       TEXT                  -- JSON: Umfang der Eingabedaten
);
"""

SYSTEM = """Du bist Verkaufsstratege für einen gewerblichen eBay.de-Händler, der vor allem gebrauchte
Videospiele verkauft. Du bekommst seinen aktuellen Bestand (aktive Festpreis-Angebote), Marktpreise der
Konkurrenz und seine Bestellhistorie.

Ziel: Ladenhüter schneller verkaufen, ohne unnötig Geld zu verschenken.

Analysiere gründlich und schlage Bündel (Pakete aus 2–6 Angeboten) vor, z. B.:
- Spielreihen / Franchise (z. B. Teil 1–3 einer Reihe) – besonders stark, wenn vollständig
- gleiche Plattform + gleiches Genre („PS3 Shooter-Paket“), Einsteiger-/Sammler-Pakete
- ein gefragter Artikel (viele Beobachter / kurz online) als Zugpferd zusammen mit Ladenhütern
- Diagnose je Artikel nutzen: „zu_teuer“ = Preisblocker (Preis runter oder günstiges Paket),
  „unsichtbar“ = Nachfrageblocker (kaum Impressionen – Preissenken hilft wenig, ideal als Beipack zu einem
  gefragten Artikel), „klick“ = wird gesehen, aber nicht angeklickt (Titel/Bild – ggf. Einzeltipp),
  „kauf“ = viele Aufrufe ohne Kauf (Interesse da – leichte Preiskorrektur oder Angebot an Beobachter).
- Muster aus der Bestellhistorie: Was haben Kunden bisher zusammen gekauft? Welche Plattformen,
  Reihen, Genres verkaufen sich gut?
Beachte: Ein Paket spart dem Käufer Versandkosten – das ist ein echtes Verkaufsargument.
Preise immer inklusive Versand vergleichen: Ein Angebot für 1 € + 8 € Versand kostet den Käufer 9 €.
Die Marktwerte in der Bestandsliste sind bereits Gesamtpreise inkl. Versand.
Bündle KEINE Artikel, die einzeln schnell und zu gutem Preis weggehen (kurz online, viele Beobachter,
Preis marktgerecht) – außer als bewusstes Zugpferd, und begründe das.

Regeln:
- Verwende nur item_ids aus der Bestandsliste. Jeder Artikel höchstens in einem Bündel.
- Untergrenze: Einkaufspreise und Mindestpreise (1 € Gewinn nach Gebühren, Porto, Steuer) stehen in der Liste.
  Ein Paket braucht nur EIN Porto (ca. 1,90–3,39 €) statt eines je Artikel – deshalb darf der Paketpreis unter der
  Summe der Einzel-Mindestpreise liegen.
- Gewinnregel (Ampel) – Ergebnis = Paketpreis − Summe EK − 12 % Gebühr − 0,45 € − Porto − Differenzsteuer:
  🟢 Ziel: mindestens {ZIEL} € Gewinn je Artikel. Das ist der Normalfall – der Händler will vorankommen.
  🟡 Knapp: 0 € bis Ziel – in Ordnung, wenn der Markt nicht mehr hergibt.
  🟠 Abverkauf: bis zu {MINUS} € Minus je Paket – NUR wenn die meisten Artikel Ladenhüter sind
     (ladenhueter = ja) und sich sonst kaum bewegen. Gute Bewertungen und freier Lagerplatz sind dem Händler
     dann mehr wert als ein paar Cent. Nutze das sparsam (wenige Pakete) und schreibe „Abverkauf“ in die Strategie.
  🔴 Mehr Minus: nie.
  Kombiniere, wo möglich, Ladenhüter mit gefragten Artikeln, damit das Paket trotzdem im grünen Bereich liegt.
- Paketpreis: unter der Summe der einzelnen Marktwerte (Ø der günstigsten Konkurrenz, falls vorhanden,
  sonst aktueller Preis), aber nicht verschenkt. Gib einen konkreten Preis in Euro an (auf ,49/,99).
- Begründe jedes Bündel kurz und konkret mit den Daten (Standzeit, Beobachter, Marktabstand, Historie).
- Zusätzlich: Tipps für einzelne Artikel, die nicht gebündelt werden sollten, aber falsch bepreist sind.
- Zusammenfassung: 4–8 Sätze zur Gesamtlage (Was läuft, was nicht, wo steckt Geld fest, Strategie).
- Deutsch, sachlich, konkret. Keine erfundenen Fakten."""

SCHEMA = {
    "type": "object",
    "properties": {
        "zusammenfassung": {"type": "string"},
        "buendel": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "preis": {"type": "number"},
                    "begruendung": {"type": "string"},
                    "strategie": {"type": "string"},
                    "prioritaet": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
                },
                "required": ["name", "item_ids", "preis", "begruendung", "strategie", "prioritaet"],
                "additionalProperties": False,
            },
        },
        "einzel_tipps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "tipp": {"type": "string"},
                    "preis": {"type": ["number", "null"]},
                },
                "required": ["item_id", "tipp", "preis"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["zusammenfassung", "buendel", "einzel_tipps"],
    "additionalProperties": False,
}

state = {"running": False}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA_SQL)
        con.execute("UPDATE analyses SET status = 'fehler', error = 'abgebrochen (Neustart)' WHERE status = 'laeuft'")


def _inventory() -> list[dict]:
    checks = market.all_checks()
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        rows = con.execute(
            "SELECT * FROM listings WHERE active = 1 AND listing_type = 'FixedPriceItem' ORDER BY title"
        ).fetchall()
    ww = wawi.for_items([r["item_id"] for r in rows])
    dg = traffic.diagnose_all(checks)
    slow_days = settings.get("slow_days")
    inv = []
    for r in rows:
        m = checks.get(r["item_id"])
        w = ww.get(r["item_id"])
        days = (now - datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))).days if r["start_time"] else None
        inv.append({
            "id": r["item_id"], "titel": r["title"], "preis": r["price"], "menge": r["quantity"],
            "tage_online": days, "beobachter": r["watch_count"],
            "markt_avg5_inkl_versand": m["avg5"] if m else None,
            "eigener_preis_inkl_versand": m["own_price"] if m else None,
            "markt_abstand_prozent": m["diff_pct"] if m else None,
            "ek": w["ek"] if w else None,
            "mindestpreis": w["min_vk"] if w else None,
            "ladenhueter": "ja" if days is not None and days >= slow_days and r["watch_count"] <= 1 else "nein",
            "impressionen_30t": dg[r["item_id"]]["impressions"] if r["item_id"] in dg else None,
            "aufrufe_30t": dg[r["item_id"]]["views"] if r["item_id"] in dg else None,
            "diagnose": dg[r["item_id"]]["key"] if r["item_id"] in dg else None,
        })
    return inv


def _prompt(inv: list[dict], orders: list[dict]) -> str:
    lines = ["# Bestand (aktive Festpreis-Angebote)",
             "id | titel | preis € | menge | tage_online | beobachter | markt_Ø5_inkl_versand | eigener_preis_inkl_versand | markt_abstand_% | einkaufspreis € | mindestpreis_einzeln € | ladenhueter | impressionen_30t | aufrufe_30t | diagnose"]
    for i in inv:
        lines.append(" | ".join(str(v if v is not None else "–") for v in i.values()))
    multi = [o for o in orders if len(o["items"]) > 1 or any(li["qty"] > 1 for li in o["items"])]
    lines += ["", f"# Bestellhistorie: {len(orders)} Bestellungen",
              f"## Zusammen gekauft ({len(multi)} Bestellungen mit mehreren Artikeln)"]
    for o in multi[:300]:
        lines.append(f"{o['date']}: " + " + ".join(f"{li['qty']}× {li['title']} ({li['price']:.2f} € + {li['shipping']:.2f} € Versand)" for li in o["items"]))
    lines += ["", "## Einzelverkäufe (neueste zuerst, max. 600)"]
    singles = sorted((o for o in orders if o not in multi), key=lambda o: o["date"], reverse=True)[:600]
    for o in singles:
        li = o["items"][0]
        lines.append(f"{o['date']}: {li['title']} ({li['price']:.2f} € + {li['shipping']:.2f} € Versand)")
    return "\n".join(lines)


def _run(analysis_id: int) -> None:
    try:
        inv = _inventory()
        try:
            orders = ebay_orders.recent_orders()
        except Exception:
            log.exception("Bestellhistorie nicht abrufbar")
            orders = []
        stats = {"bestand": len(inv), "bestellungen": len(orders),
                 "mit_marktwert": sum(1 for i in inv if i["markt_avg5_inkl_versand"])}
        system = SYSTEM.replace("{ZIEL}", f"{settings.get('min_profit_per_item'):.2f}".replace(".", ",")).replace(
            "{MINUS}", f"{settings.get('max_loss_per_bundle'):.2f}".replace(".", ","))
        client = anthropic.Anthropic()
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": _prompt(inv, orders)}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"Claude hat nicht fertig geantwortet ({response.stop_reason}).")
        result = json.loads(next(b.text for b in response.content if b.type == "text"))

        # Nur gültige, bündelbare Artikel übernehmen; jeder Artikel höchstens einmal
        valid = {i["id"]: i for i in inv}
        used: set[str] = set()
        bundles = []
        for b in result["buendel"]:
            ids = [i for i in dict.fromkeys(b["item_ids"]) if i in valid and i not in used]
            if len(ids) >= 2:
                used.update(ids)
                b["item_ids"] = ids
                b["summe_einzeln"] = round(sum(valid[i]["preis"] for i in ids), 2)
                b["marge"] = wawi.bundle_margin(ids, b["preis"])
                bundles.append(b)
        result["buendel"] = bundles
        result["einzel_tipps"] = [t for t in result["einzel_tipps"] if t["item_id"] in valid]

        u = response.usage
        cost = (u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT) / 1_000_000
        with db.connect() as con:
            con.execute("UPDATE analyses SET status='fertig', result=?, cost_usd=?, stats=? WHERE id=?",
                        (json.dumps(result, ensure_ascii=False), round(cost, 3), json.dumps(stats), analysis_id))
    except Exception as exc:
        log.exception("Analyse fehlgeschlagen")
        with db.connect() as con:
            con.execute("UPDATE analyses SET status='fehler', error=? WHERE id=?", (str(exc)[:500], analysis_id))
    finally:
        state["running"] = False


def start() -> int:
    if state["running"]:
        raise RuntimeError("Es läuft bereits eine Analyse.")
    state["running"] = True
    with db.connect() as con:
        cur = con.execute("INSERT INTO analyses(created_at, status) VALUES (?, 'laeuft')", (db.now_iso(),))
        analysis_id = cur.lastrowid
    threading.Thread(target=_run, args=(analysis_id,), daemon=True).start()
    return analysis_id


def latest() -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    out = dict(r)
    out["result"] = json.loads(r["result"]) if r["result"] else None
    out["stats"] = json.loads(r["stats"]) if r["stats"] else None
    return out
