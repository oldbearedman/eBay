"""Einstellbare Regeln (in der eigenen Datenbank gespeichert)."""
from . import db

DEFAULTS = {
    "min_profit_per_item": 1.00,   # Ziel: Gewinn je Artikel im Bündel
    "max_loss_per_bundle": 0.50,   # erlaubtes Minus je Bündel – nur für Ladenhüter
    "slow_days": 45,               # ab so vielen Tagen online (ohne Beobachter) = Ladenhüter
    "porto_3_4": 3.39,             # Paket-Porto ab 3 Artikeln (Werte aus der WaWi)
    "porto_5plus": 6.99,           # Paket-Porto ab 5 Artikeln
}

LABELS = {
    "min_profit_per_item": ("Ziel-Gewinn je Artikel (€)", "Normalfall: So viel soll jeder Artikel im Bündel mindestens bringen."),
    "max_loss_per_bundle": ("Erlaubtes Minus je Bündel (€)", "Nur für Ladenhüter-Bündel: bis zu diesem Verlust darf abverkauft werden."),
    "slow_days": ("Ladenhüter ab … Tagen", "Artikel, die so lange online sind und höchstens 1 Beobachter haben."),
    "porto_3_4": ("Porto für Pakete mit 3–4 Artikeln (€)", "Mindestens dieses Porto wird angesetzt (bzw. das teuerste Einzelporto, falls höher)."),
    "porto_5plus": ("Porto für Pakete ab 5 Artikeln (€)", "Mindestens dieses Porto wird angesetzt (bzw. das teuerste Einzelporto, falls höher)."),
}


def get(key: str) -> float:
    with db.connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return float(r["value"]) if r else float(DEFAULTS[key])


def all_values() -> dict[str, float]:
    return {k: get(k) for k in DEFAULTS}


def set_value(key: str, value: float) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))
