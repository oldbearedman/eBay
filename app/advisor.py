"""„Bündel vorschlagen“: Claude Opus 5.5 plant Pakete – und prüft jedes selbst gegen die echten Zahlen.

Ablauf: Bestand + Steckbriefe + Marktpreise + Bestellhistorie → Opus schlägt Pakete vor und ruft dabei das
Werkzeug `pakete_pruefen` auf (Gewinn-Ampel, Anteil am Marktwert, Plattformen, Dubletten …), bis alles passt.
"""
import json
import logging
import threading
from datetime import datetime, timezone

import anthropic

from . import ai, db, ebay_orders, market, meta, settings, traffic, wawi

log = logging.getLogger("ebay-manager")

MODEL = ai.MODEL
PRICE_IN, PRICE_OUT, PRICE_CACHE = 4.0, 20.0, 0.20  # $ pro Million Tokens (Claude Opus 5.5)
MAX_TURNS = 8

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

SYSTEM = """Du bist Verkaufsstratege für einen gewerblichen eBay.de-Händler (vor allem gebrauchte Videospiele,
dazu etwas Zubehör und Sammlerstücke). Deine Aufgabe: die besten PAKETE (2–6 Angebote) aus seinem Bestand bilden –
Pakete, die sich gut verkaufen UND Gewinn bringen.

QUALITÄT VOR MENGE: Liefere 8–20 Top-Pakete, sortiert nach Erfolgsaussicht. Lieber weniger, dafür überzeugende.
Ein Paket muss für einen Käufer auf den ersten Blick Sinn ergeben („das will ich komplett haben“).

PAKET-TYPEN (in dieser Rangfolge bevorzugen):
1. reihe – Teile EINER Spielreihe, am besten aufeinanderfolgend oder komplett (Teil 1–3, Trilogie).
2. franchise – gleiches Universum/Marke, verschiedene Ableger (z. B. mehrere Star-Wars- oder Lego-Spiele).
3. thema – gleiche Konsole + gleiches Genre/Thema: Rätsel/Puzzle, Rollenspiel, Shooter, Rennspiel, Sport,
   Kinder/Familie, Horror, Beat-’em-up, Strategie, Klassiker/Retro-Sammlung.
4. zugpferd – ein gefragter Artikel (Beobachter, viele Aufrufe) + thematisch passende Ladenhüter.
5. sonstiges – z. B. Zubehör-Sets (Controller + Memory Card für dieselbe Konsole), Sammlerreihen (Gläser, DVDs).

HARTE REGELN:
- Gleiche Konsole innerhalb eines Pakets. Ausnahme: PS1 + PS2 (die PS2 spielt PS1-Spiele ab). Nichtspiele
  (DVDs, Gläser, Kameras) nur mit Passendem kombinieren.
- Keine Dubletten (dasselbe Spiel zweimal) im Paket.
- Vollständigkeit nicht mischen: „cib“ (komplett) nicht mit „teil“ (ohne Anleitung / nur Disc) zusammen.
  Neu/OVP nicht mit deutlich gebrauchter Ware.
- Nur item_ids aus der Bestandsliste; jeder Artikel höchstens in einem Paket.
- Nicht bündeln: Artikel, die sich einzeln gut verkaufen („selbst_verkauft“ zum aktuellen Preis, viele
  Beobachter, kurz online, Diagnose ok) – außer bewusst als Zugpferd.

PREIS – nicht verschenken, aber verkaufbar:
- Der Preis richtet sich nach dem WERT der Artikel, nicht nach dem Einkaufspreis. Der Mindestgewinn
  ({ZIEL} € je Artikel) ist nur ein SICHERHEITSNETZ nach unten – niemals ein Ziel. Viele Artikel wurden sehr
  günstig eingekauft (unter 1 €) und sind 10–20 € wert: die sollen auch für ihren Wert verkauft werden.
  Eine lange Standzeit allein ist KEIN Grund, auf das Minimum zu gehen.
- „marktpreis“ = Median vergleichbarer Konkurrenzangebote inkl. Versand (gleiche Vollständigkeit, ähnlicher Zustand),
  „schnell“ = Preis im günstigen Drittel. „selbst_verkauft“ = echte eigene Verkaufspreise (wiegen am meisten).
- Anker = der NIEDRIGERE Wert aus (Summe der eigenen Einzelpreise inkl. Versand) und (Summe der Marktpreise).
  Paketpreis üblicherweise 85–95 % dieses Ankers. Das Paket muss für den Käufer günstiger sein als die
  Einzelangebote zusammen – sonst ergibt es keinen Sinn. Preise auf ,49 oder ,99.
- Liegen die eigenen Einzelpreise deutlich UNTER dem Markt, ist Bündeln nicht die Lösung: dann lieber
  als einzel_tipp „Preis anheben“ vorschlagen (mit konkretem Preis) statt ein Paket über den Einzelpreisen.
- Unter 75 % des Ankers nur beim Abverkauf echter Ladenhüter.
- Ein Paket braucht nur EIN Porto – dieser Vorteil erlaubt einen attraktiven Preis bei gutem Gewinn.
- Gewinn-Ampel (Ergebnis nach EK, Gebühren, Porto, Differenzsteuer):
  🟢 gut: mindestens {ZIEL} € Gewinn je Artikel – der Normalfall, der Händler will vorankommen.
  🟡 knapp: 0 € bis Ziel – nur, wenn der Markt nicht mehr hergibt.
  🟠 abverkauf: bis {MINUS} € Minus – NUR wenn die meisten Artikel Ladenhüter sind; sparsam; „Abverkauf“ in die Strategie.
  🔴 blockiert: nie vorschlagen.

PAKET RETTEN:
- Liegt ein inhaltlich gutes Paket (Reihe/Thema) im Minus oder nur knapp im Plus, prüfe, ob ein weiterer
  PASSENDER Artikel (gleiche Konsole, idealerweise gleiche Reihe/Genre) mit gutem „gewinn_einzeln“ das Paket in
  den grünen Bereich hebt – z. B. ein 2er-Paket mit −1 € + ein passendes Spiel mit +2 € Puffer = 3er-Paket mit Gewinn.
- Der Rettungsartikel muss thematisch passen (kein Lückenfüller) und sollte KEIN Selbstläufer sein
  (nicht „selbst_verkauft“ zum aktuellen Preis, nicht viele Beobachter) – ideal: guter Puffer, läuft allein langsam.
- Prüfe die gerettete Variante mit dem Werkzeug. Findet sich nichts Passendes, lieber kein Paket.

ARBEITSWEISE (wichtig):
1. Sichte Bestand, Steckbriefe, die vorab gefundenen Reihen/Genre-Gruppen und die Bestellhistorie
   (was wurde zusammen gekauft, welche Plattformen/Genres laufen).
2. Entwirf Pakete und prüfe sie mit dem Werkzeug `pakete_pruefen` (mehrere Pakete pro Aufruf).
3. Korrigiere, was die Prüfung bemängelt (Preis, Zusammensetzung) und prüfe erneut. Gib erst dann die Endantwort.
   Jedes Paket der Endantwort muss geprüft sein: Ampel gut/knapp (abverkauf nur begründet), keine Regelverstöße.

ENDANTWORT:
- zusammenfassung: 4–8 Sätze – was läuft, was nicht, wo Geld feststeckt, Strategie.
- je Paket: typ, plattform, name (kurz, verkaufsstark), verkaufsargument (1 Satz für die Anzeige, z. B.
  „Die komplette Trilogie in einem Paket – versandkostenfrei“), begruendung (mit Daten), strategie, prioritaet.
- einzel_tipps: Artikel, die NICHT ins Paket gehören, aber falsch bepreist sind (zu teuer ODER zu günstig).
Deutsch, sachlich, konkret. Keine erfundenen Fakten."""

TOOL = {
    "name": "pakete_pruefen",
    "description": (
        "Prüft Paketentwürfe gegen die echten Zahlen des Händlers: Gewinn nach EK/Gebühren/Porto/Steuer "
        "(Ampel gut/knapp/abverkauf/blockiert), Zielpreis und unterste Preisgrenze, Summe der Marktpreise und "
        "Anteil des Paketpreises daran, Vergleich mit den Einzelpreisen, Plattformen, Dubletten, "
        "gemischte Vollständigkeit, Ladenhüter-Anteil, Warnung bei verschenkten Preisen. "
        "Mehrere Pakete pro Aufruf möglich."),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "pakete": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_ids": {"type": "array", "items": {"type": "string"}},
                        "preis": {"type": "number"},
                    },
                    "required": ["item_ids", "preis"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["pakete"],
        "additionalProperties": False,
    },
}

SCHEMA = {
    "type": "object",
    "properties": {
        "zusammenfassung": {"type": "string"},
        "buendel": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "typ": {"type": "string", "enum": ["reihe", "franchise", "thema", "zugpferd", "sonstiges"]},
                    "plattform": {"type": "string"},
                    "name": {"type": "string"},
                    "verkaufsargument": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "preis": {"type": "number"},
                    "begruendung": {"type": "string"},
                    "strategie": {"type": "string"},
                    "prioritaet": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
                },
                "required": ["typ", "plattform", "name", "verkaufsargument", "item_ids", "preis",
                             "begruendung", "strategie", "prioritaet"],
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

state = {"running": False, "step": ""}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA_SQL)
        con.execute("UPDATE analyses SET status = 'fehler', error = 'abgebrochen (Neustart)' WHERE status = 'laeuft'")


# ── Daten für Opus ──────────────────────────────────────────────────────

def _inventory() -> tuple[list[dict], dict]:
    checks = market.all_checks()
    metas = meta.all_meta()
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
        iid = r["item_id"]
        m, w, mt = checks.get(iid), ww.get(iid), metas.get(iid) or {}
        days = (now - datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))).days if r["start_time"] else None
        inv.append({
            "id": iid, "titel": r["title"],
            "plattform": mt.get("platform"), "spielname": mt.get("name"), "genre": mt.get("genre"),
            "vollst": mt.get("complete") or "?", "zustand": mt.get("condition"),
            "preis": r["price"], "menge": r["quantity"], "tage": days, "beob": r["watch_count"],
            "impr30": dg[iid]["impressions"] if iid in dg else None,
            "aufr30": dg[iid]["views"] if iid in dg else None,
            "diagnose": dg[iid]["key"] if iid in dg else None,
            "marktpreis": m["avg5"] if m else None,
            "schnell": m.get("quick_price") if m else None,
            "eigen_inkl_vers": m["own_price"] if m else None,
            "selbst_verkauft": (f"{len(m['own_sales'])}x Ø {m['sold_avg']:.2f}" if m and m.get("sold_avg") else None),
            "ek": w["ek"] if w else None,
            "min_einzeln": w["min_vk"] if w else None,
            # Gewinn, den der Artikel zum aktuellen Preis allein nach allen Kosten bringt
            "gewinn_einzeln": (wawi.profit(r["price"], w["ek"], w["fee_rate"], w["versand_kosten"])["profit"]
                               if w else None),
            "ladenhueter": "ja" if days is not None and days >= slow_days and r["watch_count"] <= 1 else "nein",
        })
    return inv, meta.groups([i["id"] for i in inv], metas)


def _prompt(inv: list[dict], grp: dict, orders: list[dict]) -> str:
    cols = list(inv[0].keys()) if inv else []
    lines = ["# Bestand (aktive Festpreis-Angebote)", " | ".join(cols)]
    for i in inv:
        lines.append(" | ".join(str(v if v is not None else "–") for v in i.values()))
    lines += ["", "# Vorab gefundene Kandidaten (Hinweise, keine Pflicht)", "## Mögliche Spielreihen / Franchises"]
    lines += [f"- {k}: {', '.join(v)}" for k, v in sorted(grp["reihen"].items())] or ["- keine"]
    lines += ["## Genre-Gruppen je Plattform"]
    lines += [f"- {k}: {', '.join(v)}" for k, v in sorted(grp["genres"].items())] or ["- keine"]
    multi = [o for o in orders if len(o["items"]) > 1 or any(li["qty"] > 1 for li in o["items"])]
    lines += ["", f"# Bestellhistorie: {len(orders)} Bestellungen",
              f"## Zusammen gekauft ({len(multi)} Bestellungen mit mehreren Artikeln)"]
    for o in multi[:300]:
        lines.append(f"{o['date']}: " + " + ".join(
            f"{li['qty']}× {li['title']} ({li['price']:.2f} € + {li['shipping']:.2f} € Versand)" for li in o["items"]))
    lines += ["", "## Einzelverkäufe (neueste zuerst, max. 600)"]
    singles = sorted((o for o in orders if o not in multi), key=lambda o: o["date"], reverse=True)[:600]
    for o in singles:
        li = o["items"][0]
        lines.append(f"{o['date']}: {li['title']} ({li['price']:.2f} € + {li['shipping']:.2f} € Versand)")
    lines += ["", "Bilde jetzt die besten Pakete. Prüfe sie mit `pakete_pruefen`, bevor du antwortest."]
    return "\n".join(lines)


# ── Prüfwerkzeug ────────────────────────────────────────────────────────

def check_bundle(item_ids: list[str], price: float, inv_by_id: dict, metas: dict) -> dict:
    ids = list(dict.fromkeys(item_ids))
    unknown = [i for i in ids if i not in inv_by_id]
    if unknown:
        return {"item_ids": ids, "fehler": f"Unbekannte oder nicht bündelbare IDs: {unknown}"}
    mts = [metas.get(i) or {} for i in ids]
    platforms = {m.get("platform") for m in mts if m.get("platform")}
    names = [(m.get("name") or "").lower() for m in mts if m.get("name")]
    complete = {m.get("complete") for m in mts if m.get("complete")}
    market_sum = sum(inv_by_id[i]["marktpreis"] or inv_by_id[i]["eigen_inkl_vers"] or inv_by_id[i]["preis"] for i in ids)
    own_sum = sum(inv_by_id[i]["eigen_inkl_vers"] or inv_by_id[i]["preis"] for i in ids)
    anchor = min(market_sum, own_sum)
    mg = wawi.bundle_margin(ids, price)
    problems = []
    if price > own_sum:
        problems.append(f"teurer als die Einzelangebote zusammen ({own_sum:.2f} € inkl. Versand) – kein Vorteil für den Käufer")
    if mg.get("complete") and price < mg["min_price"] * 1.15 and market_sum > price * 1.4:
        problems.append(f"verschenkt: Preis nah am Minimum ({mg['min_price']:.2f} €), obwohl der Marktwert bei {market_sum:.2f} € liegt")
    if not meta.platforms_ok(platforms):
        problems.append(f"gemischte Plattformen: {sorted(platforms)}")
    if len(names) != len(set(names)):
        problems.append("Dublette: dasselbe Spiel mehrfach")
    if {"cib", "teil"} <= complete:
        problems.append("Vollständigkeit gemischt (cib + teil)")
    share = round(price / market_sum * 100) if market_sum else None
    anchor_share = round(price / anchor * 100) if anchor else None
    if anchor_share is not None and anchor_share < 75:
        problems.append(f"nur {anchor_share} % des Ankers ({anchor:.2f} €) – zu stark unter Wert")
    out = {
        "item_ids": ids, "preis": price, "plattformen": sorted(platforms),
        "summe_marktpreise": round(market_sum, 2), "anteil_marktwert_prozent": share,
        "summe_einzelpreise": round(sum(inv_by_id[i]["preis"] for i in ids), 2),
        "summe_einzelpreise_inkl_versand": round(own_sum, 2), "anker": round(anchor, 2),
        "anteil_anker_prozent": anchor_share,
        "ladenhueter": sum(1 for i in ids if inv_by_id[i]["ladenhueter"] == "ja"),
        "probleme": problems,
    }
    if mg.get("complete"):
        out.update(ampel=mg["level"], gewinn=mg["profit"], zielpreis=mg["min_price"],
                   untergrenze=mg["floor_price"], porto_gespart=mg["porto_saved"])
        if mg["level"] == "blockiert":
            problems.append(f"zu viel Minus – mindestens {mg['floor_price']:.2f} € nötig")
    else:
        out.update(ampel="unbekannt", hinweis=f"{len(mg['missing'])} Artikel ohne Einkaufspreis – Gewinn nicht prüfbar")
    return out


# ── Ablauf ──────────────────────────────────────────────────────────────

def _run(analysis_id: int) -> None:
    try:
        state["step"] = "Steckbriefe laden"
        meta.refresh_missing()
        state["step"] = "Bestellungen & Bestand sammeln"
        try:
            ebay_orders.store_recent()
        except Exception:
            log.exception("Bestellhistorie nicht abrufbar – nutze lokales Archiv")
        orders = ebay_orders.all_orders()
        inv, grp = _inventory()
        inv_by_id = {i["id"]: i for i in inv}
        metas = meta.all_meta()
        stats = {"bestand": len(inv), "bestellungen": len(orders),
                 "mit_marktwert": sum(1 for i in inv if i["marktpreis"]),
                 "reihen_kandidaten": len(grp["reihen"]), "pruefungen": 0}
        system = SYSTEM.replace("{ZIEL}", f"{settings.get('min_profit_per_item'):.2f}".replace(".", ",")).replace(
            "{MINUS}", f"{settings.get('max_loss_per_bundle'):.2f}".replace(".", ","))

        client = anthropic.Anthropic()
        messages = [{"role": "user", "content": _prompt(inv, grp, orders)}]
        cost = 0.0
        response = None
        for turn in range(MAX_TURNS):
            state["step"] = f"Opus plant & prüft Pakete (Runde {turn + 1})"
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=48000,
                system=system,
                tools=[TOOL],
                messages=messages,
                cache_control={"type": "ephemeral"},
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                response = stream.get_final_message()
            u = response.usage
            cost += ((u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT
                      + (getattr(u, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE
                      + (getattr(u, "cache_creation_input_tokens", 0) or 0) * PRICE_IN * 1.25) / 1_000_000)
            if response.stop_reason in ("refusal", "max_tokens"):
                raise RuntimeError(f"Claude hat nicht fertig geantwortet ({response.stop_reason}).")
            if response.stop_reason != "tool_use":
                break
            # Werkzeugaufrufe ausführen und alle Ergebnisse gesammelt zurückgeben
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    pakete = block.input.get("pakete", []) if isinstance(block.input, dict) else []
                    checked = [check_bundle(p.get("item_ids", []), float(p.get("preis", 0)), inv_by_id, metas)
                               for p in pakete]
                    stats["pruefungen"] += len(checked)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(checked, ensure_ascii=False)})
                except Exception as exc:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Fehler: {exc}", "is_error": True})
            messages.append({"role": "user", "content": results})
        else:
            raise RuntimeError("Opus ist nach mehreren Prüfrunden nicht fertig geworden.")

        state["step"] = "Ergebnis auswerten"
        result = json.loads(next(b.text for b in response.content if b.type == "text"))

        # Endkontrolle mit echten Zahlen: nur gültige, nicht blockierte Pakete; jeder Artikel höchstens einmal
        used: set[str] = set()
        final = []
        for b in result["buendel"]:
            ids = [i for i in dict.fromkeys(b["item_ids"]) if i in inv_by_id and i not in used]
            if len(ids) < 2:
                continue
            chk = check_bundle(ids, b["preis"], inv_by_id, metas)
            if chk.get("ampel") == "blockiert":
                continue
            used.update(ids)
            b.update(item_ids=ids, summe_einzeln=chk["summe_einzelpreise"], pruefung=chk,
                     marge=wawi.bundle_margin(ids, b["preis"]))
            final.append(b)
        order = {"hoch": 0, "mittel": 1, "niedrig": 2}
        final.sort(key=lambda b: (order.get(b["prioritaet"], 3), -(b["pruefung"].get("gewinn") or 0)))
        result["buendel"] = final
        result["einzel_tipps"] = [t for t in result["einzel_tipps"] if t["item_id"] in inv_by_id]

        with db.connect() as con:
            con.execute("UPDATE analyses SET status='fertig', result=?, cost_usd=?, stats=? WHERE id=?",
                        (json.dumps(result, ensure_ascii=False), round(cost, 3), json.dumps(stats), analysis_id))
    except Exception as exc:
        log.exception("Analyse fehlgeschlagen")
        with db.connect() as con:
            con.execute("UPDATE analyses SET status='fehler', error=? WHERE id=?", (str(exc)[:500], analysis_id))
    finally:
        state.update(running=False, step="")


def start() -> int:
    if state["running"]:
        raise RuntimeError("Es läuft bereits eine Analyse.")
    state.update(running=True, step="Start")
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
