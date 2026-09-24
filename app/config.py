"""Einstellungen aus der .env-Datei."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

APP_ID = os.getenv("EBAY_APP_ID", "")
CERT_ID = os.getenv("EBAY_CERT_ID", "")
DEV_ID = os.getenv("EBAY_DEV_ID", "")
RUNAME = os.getenv("EBAY_RUNAME", "")
SITE_ID = os.getenv("EBAY_SITE_ID", "77")  # 77 = eBay.de

API_BASE = "https://api.ebay.com"
AUTH_BASE = "https://auth.ebay.com"

# Wie oft die Angebote automatisch neu eingelesen werden (Stunden)
SYNC_INTERVAL_HOURS = float(os.getenv("SYNC_INTERVAL_HOURS", "6"))
