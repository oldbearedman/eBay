"""Claude ordnet die schwierigen Fälle eBay-Angebot ↔ WaWi-Artikel zu."""
import json

import anthropic

from . import ai, db, wawi

SYSTEM = """Du ordnest eBay-Angebote eines Videospiel-Händlers den Artikeln seiner Warenwirtschaft (WaWi) zu.
Zu jedem Angebot bekommst du bis zu 6 Kandidaten aus der WaWi (noch nicht verkaufte Artikel).

Denke wie ein Mensch, der seinen Lagerbestand kennt:
- Gleiches Spiel/Produkt UND gleiche Plattform sind Pflicht (PS3 ≠ PS4, Teil 2 ≠ Teil 3).
- Achte auf Zusätze: „ohne Anleitung“/„o. B.“/„nur Disc“ vs. „CIB“/„komplett“, Edition (Limited, GOTY,
  Steelbook), „neu/OVP“ vs. gebraucht. Passt ein Zusatz nicht, ist es ein anderer Artikel.
- Der eBay-Preis sollte grob zum Mindestpreis (min_vk) des WaWi-Artikels passen.
- „sicher“ = true NUR wenn genau ein Kandidat plausibel ist. Gibt es mehrere gleichwertige Kandidaten
  (z. B. dasselbe Spiel mehrfach im Lager und nichts unterscheidet sie), dann sicher = false und nimm den
  wahrscheinlichsten. Passt keiner, produktnr = null.
- Keine Vermutungen als sicher ausgeben – eine falsche feste Zuordnung verfälscht die Gewinnrechnung."""

SCHEMA = {
    "type": "object",
    "properties": {
        "zuordnungen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "produktnr": {"type": ["string", "null"]},
                    "sicher": {"type": "boolean"},
                    "grund": {"type": "string"},
                },
                "required": ["item_id", "produktnr", "sicher", "grund"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["zuordnungen"],
    "additionalProperties": False,
}


def run() -> dict:
    """Alle noch nicht bestätigten aktiven Angebote von Claude zuordnen lassen."""
    prods = wawi.products()
    with db.connect() as con:
        confirmed = {r["item_id"]: r["produktnr"] for r in con.execute(
            "SELECT item_id, produktnr FROM wawi_links WHERE confirmed = 1")}
        listings = [dict(r) for r in con.execute(
            "SELECT item_id, title, price FROM listings WHERE active = 1") if r["item_id"] not in confirmed]
    taken = set(confirmed.values())
    free = {k: v for k, v in prods.items() if k not in taken}
    if not listings:
        return {"sicher": 0, "vorschlag": 0, "keiner": 0}

    lines = []
    known = {}
    for l in listings:
        cands = [p for _, p in wawi.candidates(l["title"], free, n=6)]
        if not cands:
            continue
        known[l["item_id"]] = {c["produktnr"] for c in cands}
        lines.append(f"## Angebot {l['item_id']}: {l['title']} – {l['price']:.2f} €")
        for c in cands:
            lines.append(f"- {c['produktnr']} | {c['artikel']} | Zustand: {c['zustand']} | "
                         f"min_vk {c['min_vk']:.2f} € | Status: {c['status']}")
    if not known:
        return {"sicher": 0, "vorschlag": 0, "keiner": len(listings)}

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=ai.MODEL,
        max_tokens=32000,
        system=SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude hat nicht fertig geantwortet ({response.stop_reason}).")
    result = json.loads(next(b.text for b in response.content if b.type == "text"))

    stats = {"sicher": 0, "vorschlag": 0, "keiner": 0}
    now = db.now_iso()
    with db.connect() as con:
        for z in result["zuordnungen"]:
            iid, pnr = z["item_id"], z["produktnr"]
            if iid not in known:
                continue
            if not pnr or pnr not in known[iid]:
                stats["keiner"] += 1
                continue
            sure = z["sicher"] and pnr not in taken
            con.execute(
                """INSERT OR REPLACE INTO wawi_links(item_id, produktnr, method, score, confirmed, updated_at)
                   VALUES (?, ?, 'claude', NULL, ?, ?)""", (iid, pnr, 1 if sure else 0, now))
            if sure:
                taken.add(pnr)
            stats["sicher" if sure else "vorschlag"] += 1
    return stats
