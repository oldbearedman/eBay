"""Einstellbare Regeln (in der eigenen Datenbank gespeichert)."""
from . import db

DEFAULTS = {
    "min_profit_per_item": 1.00,   # Ziel: Gewinn je Artikel im Bündel
    "max_loss_per_bundle": 0.50,   # erlaubtes Minus je Bündel – nur für Ladenhüter
    "slow_days": 45,               # ab so vielen Tagen online (ohne Beobachter) = Ladenhüter
    "porto_einzeln": 1.80,         # 1 Spiel (Ü18 über „Alter“ KP)
    "limit_einzeln": 10.0,         # … bis zu diesem Warenwert
    "porto_zwei": 2.70,            # 2 Spiele (nicht Ü18)
    "limit_zwei": 14.0,            # … bis zu diesem Warenwert
    "porto_kp": 3.39,              # Kleinpaket: 2–5 Artikel
    "limit_kp": 25.0,              # … bis zu diesem Warenwert
    "max_kp_artikel": 5,           # … höchstens so viele Artikel
    "porto_paket": 6.99,           # darüber: Paket (kostenlos für den Käufer, im Preis enthalten)
    "wawi_konvolut": 1,            # 1 = beim Einstellen eines Bündels in der WaWi als Konvolut zusammenfassen
}

LABELS = {
    "min_profit_per_item": ("Ziel-Gewinn je Artikel (€)", "Normalfall: So viel soll jeder Artikel im Bündel mindestens bringen."),
    "max_loss_per_bundle": ("Erlaubtes Minus je Bündel (€)", "Nur für Ladenhüter-Bündel: bis zu diesem Verlust darf abverkauft werden."),
    "slow_days": ("Ladenhüter ab … Tagen", "Artikel, die so lange online sind und höchstens 1 Beobachter haben."),
    "porto_einzeln": ("Porto 1 Spiel (€)", "Deine Kosten für ein einzelnes Spiel (Ü18 über „Alter“ KP)."),
    "limit_einzeln": ("… bis Warenwert (€)", "Darüber gilt die nächste Stufe."),
    "porto_zwei": ("Porto 2 Spiele (€)", "Nur für Spiele unter 18 – Ü18 geht als Kleinpaket über „Alter“ KP."),
    "limit_zwei": ("… bis Warenwert (€)", "Darüber gilt die Kleinpaket-Stufe."),
    "porto_kp": ("Porto Kleinpaket (€)", "Deine Kosten für 2 bis „max. Artikel“ Spiele (Ü18: über „Alter“ KP)."),
    "limit_kp": ("Kleinpaket bis Warenwert (€)", "Darüber wird als Paket verschickt."),
    "max_kp_artikel": ("Kleinpaket höchstens … Artikel", "Mehr Artikel werden als Paket verschickt."),
    "porto_paket": ("Porto Paket (€)", "Deine Kosten für größere Pakete – für den Käufer kostenlos, im Preis enthalten."),
    "wawi_konvolut": ("WaWi-Konvolut anlegen (1 = ja, 0 = nein)", "Beim Einstellen eines Bündels die Artikel in der WaWi unter Block „EB<Nr.>“ zusammenfassen."),
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
