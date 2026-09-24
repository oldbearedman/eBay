"""OAuth-Anmeldung bei eBay: Zustimmungs-Link, Code-Tausch, Token-Erneuerung.

Der Refresh-Token (gültig ca. 18 Monate) liegt in data/tokens.json und wird
nie ins Git-Repo übernommen.
"""
import base64
import json
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import config

TOKEN_FILE = config.DATA_DIR / "tokens.json"

SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
]


class NotAuthorized(Exception):
    """Es gibt (noch) keinen gültigen eBay-Zugang."""


def _basic_auth() -> str:
    raw = f"{config.APP_ID}:{config.CERT_ID}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def consent_url() -> str:
    params = {
        "client_id": config.APP_ID,
        "redirect_uri": config.RUNAME,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "prompt": "login",
    }
    return f"{config.AUTH_BASE}/oauth2/authorize?{urlencode(params)}"


def extract_code(pasted: str) -> str:
    """Nimmt die komplette Adresse der eBay-Erfolgsseite (oder nur den Code)."""
    pasted = pasted.strip()
    if "code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        if "code" in qs:
            return qs["code"][0]
    return pasted


def _token_request(data: dict) -> dict:
    r = httpx.post(
        f"{config.API_BASE}/identity/v1/oauth2/token",
        headers={
            "Authorization": _basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"eBay-Token-Fehler {r.status_code}: {r.text[:300]}")
    return r.json()


def _save(tokens: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    TOKEN_FILE.chmod(0o600)  # nur für den eigenen Benutzer lesbar


def _load() -> dict | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    return None


def exchange_code(code: str) -> None:
    now = time.time()
    t = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.RUNAME,
    })
    _save({
        "access_token": t["access_token"],
        "access_expires": now + t["expires_in"] - 120,
        "refresh_token": t["refresh_token"],
        "refresh_expires": now + t.get("refresh_token_expires_in", 0),
    })


def is_authorized() -> bool:
    t = _load()
    return bool(t) and t.get("refresh_expires", 0) > time.time()


def refresh_expires_at() -> float | None:
    t = _load()
    return t.get("refresh_expires") if t else None


def access_token() -> str:
    t = _load()
    if not t or t.get("refresh_expires", 0) <= time.time():
        raise NotAuthorized("Bitte zuerst bei eBay anmelden.")
    if t["access_expires"] > time.time():
        return t["access_token"]
    new = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": t["refresh_token"],
        "scope": " ".join(SCOPES),
    })
    t["access_token"] = new["access_token"]
    t["access_expires"] = time.time() + new["expires_in"] - 120
    _save(t)
    return t["access_token"]


_app_token: dict = {}


def application_token() -> str:
    """Anwendungs-Token (ohne Nutzeranmeldung) für öffentliche Daten wie Kategorien."""
    if _app_token.get("expires", 0) > time.time():
        return _app_token["token"]
    t = _token_request({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    })
    _app_token.update(token=t["access_token"], expires=time.time() + t["expires_in"] - 120)
    return _app_token["token"]


def check_app_credentials() -> bool:
    """Prüft App ID + Cert ID über ein Anwendungs-Token (ohne Nutzeranmeldung)."""
    _token_request({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    })
    return True
