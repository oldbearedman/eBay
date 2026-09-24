"""eBay Account API (REST): Geschäftsrichtlinien des Verkäufers."""
import httpx

from . import config, ebay_auth


def _get(path: str, params: dict) -> dict:
    r = httpx.get(
        f"{config.API_BASE}/sell/account/v1/{path}",
        params=params,
        headers={"Authorization": f"Bearer {ebay_auth.access_token()}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def shipping_profiles() -> list[dict]:
    """Alle Versandrichtlinien für eBay.de: [{id, name, description}]"""
    data = _get("fulfillment_policy", {"marketplace_id": "EBAY_DE"})
    return sorted(
        (
            {"id": p["fulfillmentPolicyId"], "name": p["name"], "description": p.get("description", "")}
            for p in data.get("fulfillmentPolicies", [])
        ),
        key=lambda p: p["name"].lower(),
    )
