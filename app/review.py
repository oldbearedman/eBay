"""„Nochmal genau prüfen“: Opus nimmt EIN vorgeschlagenes Paket gründlich auseinander.

Ergebnis: bündeln / anpassen / einzeln lassen – mit Pro & Contra, Preis und Empfehlung je Artikel.
"""
import json
import logging

import anthropic

from . import advisor, ai, db, ebay_trading, market, meta, wawi

log = logging.getLogger("ebay-manager")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bundle_reviews (
    review_key  TEXT PRIMARY KEY,     -- sortierte item_ids
    created_at  TEXT NOT NULL,
    result      TEXT NOT NULL,        -- JSON
    cost_usd    REAL
);
"""

SYSTEM = """Du bist ein erfahrener eBay-Händler für gebrauchte Videospiele und prüfst EIN Paket-Vorschlag kritisch –
wie ein zweiter Experte, der Geld verdienen will. Sei ehrlich: Ein Paket ist nicht automatisch besser.

Du bekommst für jeden Artikel alle Daten: Titel, Beschreibung, Merkmale, Zustand/Vollständigkeit, eigener Preis,
konkrete Konkurrenzangebote (inkl. Versand), eigene frühere Verkäufe, Aufrufe/Impressionen/Beobachter, Standzeit,
Einkaufspreis und Mindestpreis. Dazu den Paketvorschlag und die Prüfung seiner Zahlen.

Beantworte gründlich:
1. Ergibt das Paket für einen KÄUFER Sinn? (Reihe/Thema/Konsole, Zustand passend, würde jemand genau das suchen?)
2. Verkauft sich das Paket wahrscheinlich schneller als die Einzelteile? Oder verkaufen sich manche Teile einzeln
   gut und würden das Paket nur „verschenken“? Gibt es ein Zugpferd, das einzeln mehr bringt?
3. Stimmt der Preis? Vergleiche mit den konkreten Konkurrenzangeboten. Der Mindestgewinn ist nur ein
   Sicherheitsnetz, kein Ziel – der Preis richtet sich nach dem Wert. Das Paket muss günstiger sein als die
   Einzelangebote zusammen (inkl. Versand), aber nicht unnötig billig.
4. Wäre eine andere Zusammensetzung besser? Du darfst Artikel weglassen und bis zu 2 Artikel aus der
   KANDIDATENLISTE hinzufügen (nur diese!). Besonders sinnvoll, wenn das Paket im Minus oder zu knapp ist:
   ein thematisch passender Artikel (gleiche Konsole, Reihe/Genre) mit gutem „gewinn_einzeln“ kann es retten.
   Kein Lückenfüller, kein Selbstläufer (der verkauft sich allein besser).

Nutze das Werkzeug `pakete_pruefen`, um Varianten (anderer Preis, Artikel weglassen) mit echten Zahlen zu testen,
bevor du urteilst.

Urteil:
- „buendeln“: Paket ist gut so (ggf. mit leicht angepasstem Preis).
- „anpassen“: Paket lohnt sich, aber mit anderer Zusammensetzung und/oder deutlich anderem Preis.
- „einzeln_lassen“: Einzelangebote sind besser – dann je Artikel sagen: lassen, anheben oder senken (mit Preis).
Deutsch, konkret, mit Zahlen. Keine erfundenen Fakten."""

SCHEMA = {
    "type": "object",
    "properties": {
        "urteil": {"type": "string", "enum": ["buendeln", "anpassen", "einzeln_lassen"]},
        "fazit": {"type": "string"},
        "pro": {"type": "array", "items": {"type": "string"}},
        "contra": {"type": "array", "items": {"type": "string"}},
        "risiko": {"type": "string", "enum": ["niedrig", "mittel", "hoch"]},
        "empfohlene_item_ids": {"type": "array", "items": {"type": "string"}},
        "empfohlener_preis": {"type": ["number", "null"]},
        "einzelartikel": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "empfehlung": {"type": "string", "enum": ["im_paket", "hinzufuegen", "lassen", "anheben", "senken"]},
                    "preis": {"type": ["number", "null"]},
                    "grund": {"type": "string"},
                },
                "required": ["item_id", "empfehlung", "preis", "grund"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["urteil", "fazit", "pro", "contra", "risiko", "empfohlene_item_ids", "empfohlener_preis",
                 "einzelartikel"],
    "additionalProperties": False,
}


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA_SQL)


def key(item_ids: list[str]) -> str:
    return "+".join(sorted(item_ids))


def get(item_ids: list[str]) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM bundle_reviews WHERE review_key = ?", (key(item_ids),)).fetchone()
    return {**json.loads(r["result"]), "created_at": r["created_at"], "cost_usd": r["cost_usd"]} if r else None


def all_reviews() -> dict[str, dict]:
    with db.connect() as con:
        rows = con.execute("SELECT * FROM bundle_reviews").fetchall()
    return {r["review_key"]: {**json.loads(r["result"]), "created_at": r["created_at"]} for r in rows}


def _item_block(iid: str, inv_row: dict, checks: dict, ww: dict) -> str:
    d = ebay_trading.item_details(iid)
    m = checks.get(iid) or {}
    w = ww.get(iid)
    lines = [
        f"## Artikel {iid}: {d['title']}",
        f"Preis {d['price']:.2f} € + Versand {d.get('shipping_cost') or 0:.2f} € | Zustand: {d['condition_name']} | "
        f"Vollständigkeit: {inv_row.get('vollst')} | Plattform: {inv_row.get('plattform')} | Genre: {inv_row.get('genre')}",
        f"Online seit {inv_row.get('tage')} Tagen | Beobachter {inv_row.get('beob')} | Impressionen/Aufrufe 30 Tage: "
        f"{inv_row.get('impr30')}/{inv_row.get('aufr30')} | Diagnose: {inv_row.get('diagnose')}",
        f"Einkaufspreis: {w['ek']:.2f} € | Mindestpreis einzeln: {w['min_vk']:.2f} €" if w else "Einkaufspreis: unbekannt",
        f"Marktpreis (Median vergleichbarer Angebote): {m.get('avg5')} € | günstiges Drittel: {m.get('quick_price')} € | "
        f"{m.get('found')} vergleichbare Angebote",
        "Merkmale: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in d["specifics"].items()),
        "Beschreibung (gekürzt): " + ai.plain_text(d["description"], 900).replace("\n", " "),
    ]
    if m.get("samples"):
        lines.append("Günstigste vergleichbare Konkurrenz (Gesamtpreis | Zustand | Titel):")
        lines += [f"  - {s['total']:.2f} € | {s.get('condition')} | {s['title'][:80]}" for s in m["samples"][:6]]
    if m.get("own_sales"):
        lines.append("Eigene frühere Verkäufe: " + "; ".join(f"{s['date']} {s['price']:.2f} €" for s in m["own_sales"][:5]))
    return "\n".join(lines)


def run(item_ids: list[str], price: float, name: str = "") -> dict:
    inv, _ = advisor._inventory()
    inv_by_id = {i["id"]: i for i in inv}
    ids = [i for i in dict.fromkeys(item_ids) if i in inv_by_id]
    if len(ids) < 2:
        raise ValueError("Das Paket enthält weniger als zwei aktive Artikel.")
    metas = meta.all_meta()
    checks = market.all_checks()
    ww = wawi.for_items(ids)
    first_check = advisor.check_bundle(ids, price, inv_by_id, metas)
    cands = _candidates(ids, inv_by_id, metas)
    allowed = set(ids) | {c["id"] for c in cands}

    user = "\n\n".join([
        f"# Paketvorschlag: {name or 'ohne Namen'}\nPaketpreis: {price:.2f} € (inkl. Versand)\n"
        f"Prüfung der Zahlen: {json.dumps(first_check, ensure_ascii=False)}",
        *[_item_block(i, inv_by_id[i], checks, ww) for i in ids],
        _candidate_block(cands),
        "Prüfe dieses Paket jetzt gründlich (teste Varianten mit `pakete_pruefen`) und gib dein Urteil ab.",
    ])

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user}]
    cost = 0.0
    for _ in range(6):
        with client.beta.messages.stream(
            model=ai.MODEL,
            max_tokens=32000,
            system=SYSTEM,
            tools=[advisor.TOOL],
            messages=messages,
            cache_control={"type": "ephemeral"},
            output_config={"effort": "xhigh", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        u = response.usage
        cost += ((u.input_tokens * advisor.PRICE_IN + u.output_tokens * advisor.PRICE_OUT
                  + (getattr(u, "cache_read_input_tokens", 0) or 0) * advisor.PRICE_CACHE
                  + (getattr(u, "cache_creation_input_tokens", 0) or 0) * advisor.PRICE_IN * 1.25) / 1_000_000)
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"Claude hat nicht fertig geantwortet ({response.stop_reason}).")
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            pakete = block.input.get("pakete", []) if isinstance(block.input, dict) else []
            # Nur Artikel aus diesem Paket zulassen
            checked = [advisor.check_bundle([i for i in p.get("item_ids", []) if i in allowed],
                                            float(p.get("preis", 0)), inv_by_id, metas) for p in pakete]
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(checked, ensure_ascii=False)})
        messages.append({"role": "user", "content": results})
    else:
        raise RuntimeError("Die Prüfung ist nicht fertig geworden.")

    result = json.loads(next(b.text for b in response.content if b.type == "text"))
    rec_ids = [i for i in result["empfohlene_item_ids"] if i in allowed] or ids
    result["empfohlene_item_ids"] = rec_ids
    if result["urteil"] != "einzeln_lassen" and result["empfohlener_preis"]:
        result["pruefung"] = advisor.check_bundle(rec_ids, result["empfohlener_preis"], inv_by_id, metas)
    shown = set(ids) | set(rec_ids) | {e["item_id"] for e in result["einzelartikel"] if e["item_id"] in allowed}
    result["einzelartikel"] = [e for e in result["einzelartikel"] if e["item_id"] in allowed]
    result["titel"] = {i: inv_by_id[i]["titel"] for i in shown}
    result["preis_jetzt"] = {i: inv_by_id[i]["preis"] for i in shown}
    result["hinzugefuegt"] = [i for i in rec_ids if i not in ids]
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO bundle_reviews(review_key, created_at, result, cost_usd) VALUES (?, ?, ?, ?)",
                    (key(ids), db.now_iso(), json.dumps(result, ensure_ascii=False), round(cost, 3)))
    return get(ids)



def _candidates(ids: list[str], inv_by_id: dict, metas: dict, limit: int = 30) -> list[dict]:
    """Passende Zusatzartikel: gleiche (kompatible) Konsole, nicht in anderen Vorschlägen verplant."""
    plats = {(metas.get(i) or {}).get("platform") for i in ids} - {None}
    series = {meta.series_key((metas.get(i) or {}).get("name")) for i in ids} - {None}
    genres = {g for i in ids for g in ((metas.get(i) or {}).get("genre") or "").split(", ") if g}
    a = advisor.latest()
    planned = {x for b in ((a or {}).get("result") or {}).get("buendel", []) for x in b["item_ids"]} - set(ids)
    out = []
    for iid, inv in inv_by_id.items():
        if iid in ids or iid in planned:
            continue
        m = metas.get(iid) or {}
        if not m.get("platform") or not meta.platforms_ok(plats | {m["platform"]}):
            continue
        fit = (2 if meta.series_key(m.get("name")) in series else 0) + \
              (1 if genres & set((m.get("genre") or "").split(", ")) else 0)
        out.append({**inv, "passung": fit})
    out.sort(key=lambda c: (-c["passung"], -(c.get("gewinn_einzeln") or -99)))
    return out[:limit]


def _candidate_block(cands: list[dict]) -> str:
    if not cands:
        return "# Kandidatenliste zum Hinzufügen\n(keine passenden Artikel verfügbar)"
    cols = ["id", "titel", "plattform", "genre", "vollst", "preis", "marktpreis", "gewinn_einzeln",
            "tage", "beob", "diagnose", "selbst_verkauft", "passung"]
    lines = ["# Kandidatenliste zum Hinzufügen (passung: 2 = gleiche Reihe, 1 = gleiches Genre)", " | ".join(cols)]
    for c in cands:
        lines.append(" | ".join(str(c.get(k) if c.get(k) is not None else "–") for k in cols))
    return "\n".join(lines)
