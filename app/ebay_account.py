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
                "free_text": a.get("aspectConstraint", {}).get("aspectMode") == "FREE_TEXT",
            }
            for a in r.json().get("aspects", [])
        }
    return _aspect_cache[category_id]


_policy_cache: dict[str, tuple[float, list]] = {}


def _cached(kind: str, fn) -> list[dict]:
    import time
    hit = _policy_cache.get(kind)
    if hit and hit[0] > time.time():
        return hit[1]
    data = fn()
    _policy_cache[kind] = (time.time() + 600, data)
    return data


def shipping_profiles() -> list[dict]:
    """Versandrichtlinien für eBay.de mit Käuferkosten, Versandart, Altersprüfung und geschätzten eigenen Kosten."""
    def load():
        data = _get("fulfillment_policy", {"marketplace_id": "EBAY_DE"})
        paid: dict[str, float] = {}   # Versandart → höchster Preis, den ein Profil dafür verlangt (≈ eigene Kosten)
        out = []
        for p in data.get("fulfillmentPolicies", []):
            svcs = [s for o in p.get("shippingOptions", []) if o.get("optionType") == "DOMESTIC"
                    for s in o.get("shippingServices", []) if s.get("shippingServiceCode") != "DE_Pickup"]
            for s in svcs:
                if not s.get("freeShipping"):
                    v = float((s.get("shippingCost") or {}).get("value", 0) or 0)
                    paid[s["shippingServiceCode"]] = max(paid.get(s["shippingServiceCode"], 0.0), v)
            main = svcs[0] if svcs else {}
            code = main.get("shippingServiceCode", "")
            buyer = 0.0 if main.get("freeShipping") else float((main.get("shippingCost") or {}).get("value", 0) or 0)
            out.append({"id": p["fulfillmentPolicyId"], "name": p["name"], "description": p.get("description", ""),
                        "buyer_cost": buyer, "service": code, "age_check": "alterssicht" in code.lower(),
                        "handling_days": (p.get("handlingTime") or {}).get("value")})
        for pr in out:
            pr["own_cost"] = paid.get(pr["service"])
        return sorted(out, key=lambda p: p["name"].lower())
    return _cached("shipping", load)


def return_profiles() -> list[dict]:
    def load():
        data = _get("return_policy", {"marketplace_id": "EBAY_DE"})
        return [{"id": p["returnPolicyId"], "name": p["name"],
                 "info": (f"{(p.get('returnPeriod') or {}).get('value')} Tage, Rückversand zahlt "
                          f"{'Käufer' if p.get('returnShippingCostPayer') == 'BUYER' else 'Verkäufer'}")
                 if p.get("returnsAccepted") else "keine Rücknahme"}
                for p in data.get("returnPolicies", [])]
    return _cached("return", load)


def payment_profiles() -> list[dict]:
    def load():
        data = _get("payment_policy", {"marketplace_id": "EBAY_DE"})
        return [{"id": p["paymentPolicyId"], "name": p["name"],
                 "info": "sofortige Bezahlung" if p.get("immediatePay") else ""}
                for p in data.get("paymentPolicies", [])]
    return _cached("payment", load)


def profile_by_id(profile_id: str) -> dict | None:
    try:
        return next((p for p in shipping_profiles() if p["id"] == profile_id), None)
    except Exception:
        return None
