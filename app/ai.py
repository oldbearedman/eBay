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


TITLE_TAIL = {"in", "mit", "und", "für", "ohne", "inkl", "inkl.", "von", "der", "die", "das", "&", "+", "-", "–", "/"}


# Hausregel: Fassung/Herkunft gehört nie in den Titel (steht bei Bedarf sachlich in der Beschreibung)
TITLE_BANNED = re.compile(
    r"\(?\b(?:pegi(?:\s*-?\s*\d+)?|import(?:version|ware|spiel)?|imported|ntsc(?:\s*-?\s*(?:u/c|u|j|c))?"
    r"|(?:us|uk|eu|at|ch|jp|jap|asia)\s*-?\s*(?:version|fassung|import|cover)|esrb|mature(?:\s*17\+?)?"
    r"|m\s*17\+?|(?:österreich|austria)\w*(?:\s*-?\s*(?:version|fassung))?)(?!\w)\)?", re.I)


def clean_title(title: str) -> str:
    """PEGI, Import, NTSC, US-/UK-Version usw. aus dem Titel entfernen und sauber kürzen."""
    t = TITLE_BANNED.sub(" ", title)
    t = re.sub(r"\(\s*\)|\[\s*\]", " ", t)                       # leere Klammern
    t = re.sub(r"\s*([|/–-])\s*(?=[|/–-]|$)", " ", t)              # hängende Trenner
    t = re.sub(r"\s{2,}", " ", t).strip(" |/–-,")
    return cut_title(t)


def cut_title(title: str) -> str:
    """Auf 80 Zeichen kürzen – am Wortende, ohne hängendes „in“, „mit“, „und“ …"""
    if len(title) <= 80:
        return title
    words = title[:81].split()
    if len(title) > 80 and not title[80].isspace():
        words = words[:-1]          # angeschnittenes Wort weg
    while words and words[-1].lower() in TITLE_TAIL:
        words.pop()
    return " ".join(words)


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
    log_usage("buendeltext", response)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claudes Antwort wurde abgeschnitten.")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    title = data["title"].strip()
    if len(title) > 80:
        title = cut_title(title)
    return {"title": clean_title(title), "description": data["description_html"].strip()}


# ── Handy: Artikel auf Fotos erkennen, Einzelangebot schreiben ─────────

IDENTIFY_SYSTEM = """Du hilfst einem gewerblichen eBay.de-Händler für gebrauchte Videospiele, Konsolen, Zubehör, Bücher u. a.
Du bekommst Fotos EINES Artikels und seine Lagerliste (Warenwirtschaft). Der Händler bestätigt deine Erkennung
gleich selbst – sei präzise und lass Felder leer, die du nicht sicher erkennst.

Aufgabe:
1. Erkenne den Artikel: offizieller Titel, Plattform, Edition (z. B. Platinum, Classics, Essentials,
   Collector's, Steelbook – sonst leer), Region (PAL/NTSC), Sprache/Land (nur wenn erkennbar, z. B. deutsches
   Cover/USK-Logo), Altersfreigabe, Genre, Herausgeber, Erscheinungsjahr.
   Altersfreigabe: NUR das deutsche USK-Logo zählt. Trägt das Cover stattdessen nur PEGI oder ESRB (Import,
   österreichische/UK-/US-Fassung), ist usk = "keine" – rechtlich gilt der Artikel dann als nicht gekennzeichnet
   und darf nur mit Altersprüfung verschickt werden. usk = "" nur, wenn das Cover nicht zu sehen ist.
   fremdfassung = true, sobald es KEINE deutsche Fassung ist: Cover mit PEGI- oder ESRB-Logo statt USK, NTSC,
   UK-/US-/Import-Cover – auch wenn auf der Disc zusätzlich ein USK-Logo steht (dann usk = diese Zahl).
   EAN nur, wenn der Barcode mit Ziffern klar lesbar ist – sonst "".
2. Lagerliste: Welcher Eintrag ist genau dieser Artikel (gleiches Spiel, gleiche Plattform, passende Edition)?
   Gib dessen Produktnummer zurück, sonst "". Bei mehreren gleichen Einträgen nimm den ersten.
   Sicherheit: hoch = eindeutig; mittel = sehr wahrscheinlich; niedrig = geraten; keine = kein Eintrag passt.
   Bücher: name = Buchtitel, autor, herausgeber = Verlag, format (Taschenbuch, Gebundene Ausgabe, Broschiert …),
   ean = ISBN-13 vom Barcode; plattform leer, usk leer, fremdfassung false.
   Maße und Gewicht des Artikels OHNE Verpackung schätzen (für die Versandwahl): aus bekannten Produktdaten
   (Buchformat, Seitenzahl, Hardcover/Taschenbuch, Konsolen-/Zubehörmaße) bzw. vom Foto. Eher knapp großzügig
   schätzen als zu klein. Bei Videospielen in Standardhülle genügt eine grobe Angabe.
3. Unsicherheiten: nur zur ERKENNUNG (z. B. „Edition nicht erkennbar“, „zweites Spiel auf Foto 1“).
4. Suchbegriff für die eBay-Suche nach Vergleichsangeboten: Spielname + Plattform-Kurzform, ohne Füllwörter."""

IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "erkannt": {"type": "boolean", "description": "false, wenn auf den Fotos kein Artikel eindeutig erkennbar ist"},
        "artikel_typ": {"type": "string", "enum": ["videospiel", "konsole", "zubehoer", "film_musik", "buch", "sonstiges"]},
        "name": {"type": "string", "description": "offizieller Titel (deutsche PAL-Fassung, falls vorhanden)"},
        "plattform": {"type": "string", "description": "Wert aus der Plattform-Liste oder \"\""},
        "edition": {"type": "string"},
        "region": {"type": "string", "enum": ["PAL", "NTSC-U/C (US/Canada)", "NTSC-J (Japan)", "unbekannt"]},
        "sprache": {"type": "string"},
        "ean": {"type": "string"},
        "usk": {"type": "string", "enum": ["", "0", "6", "12", "16", "18", "keine"],
                "description": "Zahl vom USK-Logo; \"keine\" = Artikel trägt sichtbar KEIN USK-Logo (nur PEGI/ESRB, Import); \"\" = nicht erkennbar"},
        "fremdfassung": {"type": "boolean",
                         "description": "true = PEGI/ESRB-Cover, NTSC oder Import-Fassung (auch wenn auf der Disc USK steht)"},
        "genre": {"type": "string"},
        "herausgeber": {"type": "string"},
        "erscheinungsjahr": {"type": "string"},
        "autor": {"type": "string", "description": "bei Büchern (sonst leer)"},
        "format": {"type": "string", "description": "bei Büchern z. B. Taschenbuch, Gebundene Ausgabe (sonst leer)"},
        "masse_cm": {"type": "object", "description": "geschätzte Maße ohne Verpackung in cm",
                     "properties": {"l": {"type": "number"}, "b": {"type": "number"}, "h": {"type": "number"}},
                     "required": ["l", "b", "h"], "additionalProperties": False},
        "gewicht_g": {"type": "integer", "description": "geschätztes Gewicht ohne Verpackung in Gramm"},
        "wawi_produktnr": {"type": "string"},
        "wawi_sicherheit": {"type": "string", "enum": ["hoch", "mittel", "niedrig", "keine"]},
        "unsicherheiten": {"type": "array", "items": {"type": "string"}},
        "suchbegriff": {"type": "string"},
    },
    "required": ["erkannt", "artikel_typ", "name", "plattform", "edition", "region", "sprache", "ean", "usk",
                 "fremdfassung", "genre", "herausgeber", "erscheinungsjahr", "autor", "format", "masse_cm", "gewicht_g",
                 "wawi_produktnr", "wawi_sicherheit", "unsicherheiten",
                 "suchbegriff"],
    "additionalProperties": False,
}

CONDITION_SYSTEM = """Du beurteilst für einen gewerblichen eBay.de-Händler Lieferumfang und Zustand EINES gebrauchten
Artikels. Der Artikel ist bereits bestätigt (Steckbrief). Du bekommst die Fotos und die Zustandsnotiz des Händlers.

- Lieferumfang: Was ist tatsächlich zu sehen bzw. laut Notiz dabei (Hülle, Anleitung, Datenträger/Modul)?
  Nicht Sichtbares und nicht Genanntes = "unklar".
- Zustand: Die Notiz des Händlers hat Vorrang, die Fotos ergänzen. Nur Sichtbares bzw. Genanntes beschreiben,
  sachlich und ohne Beschönigung (z. B. „Disc mit leichten Gebrauchsspuren“, „Hülle mit Riss am Scharnier“).
- Unsicherheiten: nur zu Zustand/Lieferumfang, was der Händler vor dem Einstellen prüfen sollte."""

CONDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "umfang": {
            "type": "object",
            "properties": {
                "huelle": {"type": "string", "enum": ["ja", "nein", "unklar"]},
                "anleitung": {"type": "string", "enum": ["ja", "nein", "unklar"]},
                "datentraeger": {"type": "string", "enum": ["ja", "nein", "unklar"]},
                "sonstiges": {"type": "string", "description": "weiteres Sichtbares, z. B. Poster, Karte, Kabel"},
            },
            "required": ["huelle", "anleitung", "datentraeger", "sonstiges"],
            "additionalProperties": False,
        },
        "zustand": {"type": "string", "enum": ["neu", "neuwertig", "sehr_gut", "gut", "akzeptabel", "defekt"]},
        "zustand_details": {"type": "array", "items": {"type": "string"}},
        "unsicherheiten": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["umfang", "zustand", "zustand_details", "unsicherheiten"],
    "additionalProperties": False,
}


def _images(photos: list[bytes], limit: int = 8) -> list[dict]:
    import base64
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(data).decode()}} for data in photos[:limit]]


def log_usage(task: str, response) -> None:
    """Token-Verbrauch je Aufruf festhalten (für die Kostenübersicht)."""
    from . import db
    u = response.usage
    try:
        with db.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS ai_usage (ts TEXT NOT NULL, task TEXT, model TEXT,
                           input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER)""")
            con.execute("INSERT INTO ai_usage VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (db.now_iso(), task, response.model, u.input_tokens, u.output_tokens,
                         getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0))
    except Exception:
        pass


def _ask(system: str, content: list, schema: dict, effort: str, max_tokens: int = 16000,
         task: str = "", model: str | None = None) -> dict:
    client = anthropic.Anthropic()
    model = model or MODEL
    fmt = {"format": {"type": "json_schema", "schema": schema}}
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config=fmt if "haiku" in model else {"effort": effort, **fmt},   # Haiku kennt kein „effort“
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    log_usage(task, response)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claudes Antwort wurde abgeschnitten.")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def identify_item(photos: list[bytes], stock: list[dict], platforms: list[str], model: str | None = None) -> dict:
    """Schritt 1 – was ist das? photos: JPEG-Bytes; stock: [{produktnr, artikel, zustand}] (WaWi „Im Lager“)."""
    stock_text = "\n".join(f"{s['produktnr']} | {s['artikel']} | {s['zustand']}" for s in stock) or "(leer)"
    content = [
        {"type": "text", "text": "Plattform-Liste (eBay-Werte): " + "; ".join(platforms)
                                 + "\n\nLagerliste (Produktnr | Artikel | Zustand):\n" + stock_text,
         # Liste ist stabil (alle offenen WaWi-Artikel, ohne Status) → 1 h im Zwischenspeicher, fast kostenlos
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        *_images(photos),
        {"type": "text", "text": "Welcher Artikel ist das?"},
    ]
    return _ask(IDENTIFY_SYSTEM, content, IDENTIFY_SCHEMA, "medium", task="erkennen", model=model)


def assess_condition(photos: list[bytes], note: str, facts: dict, model: str | None = None) -> dict:
    """Schritt 2 – Lieferumfang und Zustand aus Fotos + Notiz des Händlers."""
    content = [
        {"type": "text", "text": "Steckbrief (vom Händler bestätigt):\n" + json.dumps(facts, ensure_ascii=False, indent=1)},
        *_images(photos),
        {"type": "text", "text": f"Zustandsnotiz des Händlers: {note.strip() or '(keine)'}"},
    ]
    return _ask(CONDITION_SYSTEM, content, CONDITION_SCHEMA, "medium", 8000, task="zustand", model=model)


# ── Ankauf: ganzes Konvolut auf Fotos erkennen ─────────────────────────

LOT_SYSTEM = """Du erfasst für einen gewerblichen Händler (gebrauchte Videospiele, Konsolen, Zubehör) einen Ankauf:
Auf den Fotos ist ein KONVOLUT zu sehen. Liste JEDES physische Exemplar auf – das wird die Warenwirtschaft.

- Fotos können sich überschneiden (Übersicht + Nahaufnahmen, Vorder- und Rückseiten): dasselbe Exemplar nur
  EINMAL zählen. Mehrere gleiche Exemplare desselben Spiels → eine Zeile mit anzahl > 1.
- name: offizieller Titel (deutsche Fassung, falls erkennbar), ohne Plattform. Nicht lesbar → trotzdem aufführen,
  z. B. „unbekanntes PS2-Spiel (Foto 3, rechts unten)“.
- plattform: Kurzform (PS1, PS2, PS3, PS4, PS5, PSP, PS Vita, Xbox, Xbox 360, Xbox One, Xbox Series X, Switch,
  Wii, Wii U, DS, 3DS, GameCube, N64, SNES, NES, Game Boy, GBA, Mega Drive, Dreamcast, PC, …) oder "".
- umfang: komplett (Hülle, Anleitung, Disc/Modul) | ohne Anleitung | nur Disc/Modul | Hülle ohne Disc |
  versiegelt | Gerät | Sonstiges. Was nicht zu sehen ist, nicht erfinden – im Zweifel „ohne Anleitung“ und in
  unsicherheiten vermerken.
- zustand: nur nach Sichtbarem; im Zweifel „Gebraucht“.
- fremdfassung = true bei PEGI-/ESRB-Cover, NTSC oder Import (keine deutsche USK-Fassung).
- hinweis: kurz, nur wenn wichtig (z. B. „Hülle gebrochen“, „Platinum“, „Promo“).
- unsicherheiten: was der Händler prüfen sollte (z. B. „Foto 2 unscharf – 3 Titel geraten“)."""

LOT_SCHEMA = {
    "type": "object",
    "properties": {
        "artikel": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "plattform": {"type": "string"},
                    "art": {"type": "string", "enum": ["videospiel", "konsole", "zubehoer", "film_musik", "buch", "sonstiges"]},
                    "umfang": {"type": "string", "enum": ["komplett", "ohne Anleitung", "nur Disc/Modul", "Hülle ohne Disc",
                                                          "versiegelt", "Gerät", "Sonstiges"]},
                    "zustand": {"type": "string", "enum": ["Neu", "Gebraucht - Hervorragend", "Gebraucht - Gut",
                                                           "Gebraucht - Akzeptabel", "Gebraucht", "Defekt / Ersatzteile"]},
                    "anzahl": {"type": "integer"},
                    "fremdfassung": {"type": "boolean"},
                    "hinweis": {"type": "string"},
                },
                "required": ["name", "plattform", "art", "umfang", "zustand", "anzahl", "fremdfassung", "hinweis"],
                "additionalProperties": False,
            },
        },
        "unsicherheiten": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["artikel", "unsicherheiten"],
    "additionalProperties": False,
}


def identify_lot(photos: list[bytes], hint: str = "") -> dict:
    """Alle Artikel eines Konvoluts von den Fotos → {artikel: [...], unsicherheiten: [...]}"""
    content = []
    for n, img in enumerate(_images(photos, 20), 1):
        content += [{"type": "text", "text": f"Foto {n}:"}, img]
    content.append({"type": "text", "text": f"Hinweis des Händlers: {hint.strip() or '(keiner)'}\n\nBitte alle Artikel auflisten."})
    return _ask(LOT_SYSTEM, content, LOT_SCHEMA, "high", 32000, task="ankauf")


SINGLE_SYSTEM = """Du schreibst ein eBay.de-Angebot für einen gewerblichen Verkäufer (ein einzelner Artikel).

Regeln:
- Verwende ausschließlich die gelieferten Fakten (Steckbrief, Lieferumfang, Zustand, Notiz des Händlers).
  Erfinde nichts dazu – keine Zustände, kein Zubehör, keine Spielinhalte, die nicht in den Daten stehen.
  Was „unklar“ ist oder unter „UNSICHERE Punkte“ steht, wird nicht als Tatsache genannt (z. B. keine
  Sprachangabe wie „deutsch“, wenn die Sprache unsicher ist).
- Titel: HÖCHSTENS 80 Zeichen (Leerzeichen zählen mit – lieber einen Suchbegriff weglassen, als dass der Titel
  abgeschnitten wird), Deutsch. Spielname und Plattform nach vorne, dann wichtige Suchbegriffe
  (z. B. PAL, deutsch, OVP/komplett mit Anleitung, Edition). Keine Großbuchstaben-Wörter nur zur Betonung,
  keine Sonderzeichen-Spielereien, keine Zustandswörter wie „TOP“.
  NIEMALS im Titel: PEGI, Import, NTSC, US-/UK-/EU-Version, ESRB, Mature o. Ä. – auch wenn es so im Steckbrief
  steht. Region/Fassung wird (falls wichtig) nur sachlich in der Beschreibung erwähnt.
- Beschreibung: schlichtes HTML (nur <h2>, <h3>, <p>, <ul>, <li>, <b>, <br>). Aufbau: kurze Einleitung,
  „Lieferumfang“ als Liste, „Zustand“ als Liste (sachlich, auch Mängel klar benennen), optional kurz „Zum Spiel“
  mit Eckdaten (Genre, Erscheinungsjahr) – nur, wenn sie im Steckbrief stehen.
- KEINE Angaben zu Versand, Preisen, Steuern, Rücknahme oder Altersprüfung – diese Hinweise fügt das System selbst an.
- Keine Umwelt- oder Nachhaltigkeitsaussagen und keine Garantieversprechen (EU-Richtlinie 2024/825, EmpCo).
- Ton: sachlich, freundlich, Sie-Form.
- Merkmale: fülle die gelisteten eBay-Merkmale, soweit die Fakten es hergeben. Bei Merkmalen mit Werteliste
  nimm genau einen passenden Listenwert. Lass Merkmale weg, die du nicht sicher weißt. Nie „Herstellergarantie“."""

SINGLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Angebotstitel, höchstens 80 Zeichen"},
        "description_html": {"type": "string"},
        "specifics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "values"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "description_html", "specifics"],
    "additionalProperties": False,
}

SKIP_ASPECTS = {"Herstellergarantie", "Ursprungsland", "Maßeinheit", "Anzahl der Einheiten"}


def write_single_text(facts: dict, note: str, condition_name: str, aspects: dict[str, dict],
                      doubts: list[str] | None = None, model: str | None = None) -> dict:
    """→ {title, description, specifics: {Name: [Werte]}}"""
    lines = [f"eBay-Zustand: {condition_name}", f"Notiz des Händlers: {note.strip() or '(keine)'}",
             "Steckbrief (aus den Fotos erkannt):", json.dumps(facts, ensure_ascii=False, indent=1), "",
             "UNSICHERE Punkte – weder in Titel, Beschreibung noch Merkmalen als Tatsache nennen:",
             *([f"- {d}" for d in doubts] if doubts else ["- (keine)"]), "",
             "eBay-Merkmale der Kategorie (Name – Pflicht? – mehrere Werte? – Werteliste):"]
    for name, rule in aspects.items():
        if name in SKIP_ASPECTS:
            continue
        vals = rule.get("values") or []
        lines.append(f"- {name} – {'Pflicht' if rule['required'] else 'optional'} – {'mehrere' if rule['multi'] else 'einer'}"
                     + (f" – {' | '.join(vals)}" if 0 < len(vals) <= 160 else " – freier Text"))
    data = _ask(SINGLE_SYSTEM, [{"type": "text", "text": "\n".join(lines)}], SINGLE_SCHEMA, "low", 8000, task="text", model=model)
    title = clean_title(data["title"].strip())
    specifics = {}
    for s in data["specifics"]:
        rule = aspects.get(s["name"])
        vals = [v.strip() for v in s["values"] if v and v.strip()]
        if not rule or not vals or s["name"] in SKIP_ASPECTS:
            continue
        specifics[s["name"]] = vals if rule["multi"] else vals[:1]
    return {"title": title, "description": data["description_html"].strip(), "specifics": specifics}
