"""Titel und Beschreibungen mit Claude schreiben."""
import html
import json
import os
import re

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM = """Du schreibst Angebotstexte für einen gewerblichen eBay.de-Verkäufer.
Aufgabe: Aus mehreren Einzelangeboten wird ein Bündel-Angebot (ein Paket, alle Artikel zusammen).

Regeln:
- Verwende ausschließlich Fakten aus den gelieferten Angebotsdaten. Erfinde nichts dazu
  (keine Zustände, kein Zubehör, keine Versprechen, die nicht in den Daten stehen).
- Titel: höchstens 80 Zeichen, Deutsch, suchmaschinenfreundlich. Wichtigste Suchbegriffe
  nach vorne (z. B. Plattform, Spielnamen, Marke), klar erkennbar, dass es ein Paket ist.
  Keine Sonderzeichen-Spielereien, keine Großbuchstaben-Wörter nur zur Betonung.
- Beschreibung: schlichtes HTML (nur <h2>, <h3>, <p>, <ul>, <ol>, <li>, <b>, <br>).
  Aufbau: kurze Einleitung, nummerierte Liste aller Artikel mit Zustand (die Nummern
  entsprechen der Nummerierung auf dem Collage-Foto), danach je Artikel die wichtigen
  Details aus seiner Originalbeschreibung, knapp zusammengefasst.
  Rechtliche Hinweise aus den Originalbeschreibungen (z. B. Altersfreigabe, Gewährleistung,
  Versandhinweise) übernimm einmal am Ende – wortgleich, nicht umformuliert.
- Versand: Angaben zum Versand AUSSCHLIESSLICH aus der Zeile „Versand für den Käufer“ übernehmen. Schreibe nur
  „versandkostenfrei“, wenn dort „kostenlos“ steht – auch wenn die Paketidee etwas anderes sagt.
- Keine Preise in Titel oder Beschreibung (Preise können sich ändern).
- Keine Umwelt- oder Nachhaltigkeitsaussagen („umweltfreundlich“, „nachhaltig“, „klimaneutral“, „zweites Leben“ o. Ä.)
  und keine Garantieversprechen – beides ist seit der EU-Richtlinie 2024/825 (EmpCo) nur mit Beleg zulässig.
- Ton: sachlich, freundlich, Sie-Form."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Angebotstitel, höchstens 80 Zeichen"},
        "description_html": {"type": "string", "description": "Beschreibung als einfaches HTML"},
    },
    "required": ["title", "description_html"],
    "additionalProperties": False,
}


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def plain_text(desc_html: str, limit: int = 2000) -> str:
    """Originalbeschreibung als Klartext (für den Prompt)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", desc_html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def write_bundle_text(sources: list[dict], price: float, hint: str | None = None,
                      shipping_note: str | None = None) -> dict:
    """sources: [{title, condition, specifics, description}] → {title, description_html}"""
    lines = [f"Anzahl Artikel: {len(sources)}",
             f"Versand für den Käufer: {shipping_note or 'unbekannt – keine Aussage zum Versand machen'}"]
    if hint:
        lines.append(f"Paketidee (Idee aufgreifen, z. B. „Trilogie“, „Rätsel-Paket“ – Versandangaben daraus ignorieren): {hint}")
    lines.append("")
    for i, s in enumerate(sources, 1):
        lines += [
            f"### Artikel {i}",
            f"Titel: {s['title']}",
            f"Zustand: {s['condition']}",
            "Merkmale: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in s["specifics"].items()),
            "Originalbeschreibung:",
            s["description"],
            "",
        ]

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claudes Antwort wurde abgeschnitten.")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    title = data["title"].strip()
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]
    return {"title": title, "description": data["description_html"].strip()}
