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


_aspect_cache: dict[str, dict] = {}


def category_aspects(category_id: str) -> dict[str, dict]:
    """Merkmale einer Kategorie auf eBay.de: {Name: {"multi": bool, "required": bool}}"""
    if category_id not in _aspect_cache:
        r = httpx.get(
            f"{config.API_BASE}/commerce/taxonomy/v1/category_tree/77/get_item_aspects_for_category",
            params={"category_id": category_id},
            headers={"Authorization": f"Bearer {ebay_auth.application_token()}"},
            timeout=30,
        )
        r.raise_for_status()
        _aspect_cache[category_id] = {
            a["localizedAspectName"]: {
                "multi": a.get("aspectConstraint", {}).get("itemToAspectCardinality") == "MULTI",
                "required": bool(a.get("aspectConstraint", {}).get("aspectRequired")),
            }
            for a in r.json().get("aspects", [])
        }
    return _aspect_cache[category_id]


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
