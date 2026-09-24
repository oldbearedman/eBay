"""Lokale SQLite-Datenbank."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

DB_FILE = config.DATA_DIR / "ebay.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    price        REAL NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'EUR',
    quantity     INTEGER NOT NULL DEFAULT 1,
    start_time   TEXT,
    watch_count  INTEGER NOT NULL DEFAULT 0,
    url          TEXT,
    image_url    TEXT,
    listing_type TEXT,
    sku          TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    synced_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    count     INTEGER,
    message   TEXT
);
"""


@contextmanager
def connect():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
