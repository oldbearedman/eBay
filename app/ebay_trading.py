"""Zugriff auf die eBay Trading API (XML).

Die Trading API sieht – anders als die neuere Inventory API – auch Angebote,
die über die eBay-Webseite eingestellt wurden.
"""
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from . import config, ebay_auth

NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
COMPAT_LEVEL = "1349"


class EbayError(Exception):
    pass


def call(verb: str, inner_xml: str) -> ET.Element:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<{verb}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
        "<ErrorLanguage>de_DE</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"{inner_xml}</{verb}Request>"
    )
    r = httpx.post(
        f"{config.API_BASE}/ws/api.dll",
        headers={
            "X-EBAY-API-SITEID": config.SITE_ID,
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
            "X-EBAY-API-CALL-NAME": verb,
            "X-EBAY-API-IAF-TOKEN": ebay_auth.access_token(),
            "Content-Type": "text/xml; charset=utf-8",
        },
        content=body.encode("utf-8"),
        timeout=60,
    )
    root = ET.fromstring(r.content)
    ack = root.findtext("e:Ack", namespaces=NS)
    if ack not in ("Success", "Warning"):
        msgs = [
            (e.findtext("e:LongMessage", namespaces=NS) or e.findtext("e:ShortMessage", namespaces=NS) or "").strip()
            for e in root.findall("e:Errors", NS)
            if e.findtext("e:SeverityCode", namespaces=NS) == "Error"
        ]
        raise EbayError(f"{verb}: " + " | ".join(msgs or [r.text[:300]]))
    return root


def _t(el: ET.Element, path: str) -> str | None:
    return el.findtext(path, namespaces=NS)


def _parse_item(it: ET.Element) -> dict:
    price_el = it.find("e:SellingStatus/e:CurrentPrice", NS)
    if price_el is None:
        price_el = it.find("e:BuyItNowPrice", NS)
    return {
        "item_id": _t(it, "e:ItemID"),
        "title": _t(it, "e:Title") or "",
        "price": float(price_el.text) if price_el is not None else 0.0,
        "currency": price_el.get("currencyID", "EUR") if price_el is not None else "EUR",
        "quantity": int(_t(it, "e:QuantityAvailable") or _t(it, "e:Quantity") or 0),
        "start_time": _t(it, "e:ListingDetails/e:StartTime"),
        "watch_count": int(_t(it, "e:WatchCount") or 0),
        "url": _t(it, "e:ListingDetails/e:ViewItemURL"),
        "image_url": _t(it, "e:PictureDetails/e:GalleryURL"),
        "listing_type": _t(it, "e:ListingType"),
        "sku": _t(it, "e:SKU"),
    }


def get_active_listings() -> list[dict]:
    """Alle aktiven Angebote, seitenweise (je 200)."""
    items: list[dict] = []
    page = 1
    while True:
        root = call("GetMyeBaySelling", (
            "<ActiveList><Include>true</Include><IncludeWatchCount>true</IncludeWatchCount>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"
            "</ActiveList>"
            "<DetailLevel>ReturnAll</DetailLevel>"
        ))
        active = root.find("e:ActiveList", NS)
        if active is None:
            break
        for it in active.findall("e:ItemArray/e:Item", NS):
            items.append(_parse_item(it))
        total_pages = int(_t(active, "e:PaginationResult/e:TotalNumberOfPages") or 1)
        if page >= total_pages:
            break
        page += 1
    return items


def get_item(item_id: str) -> ET.Element:
    """Vollständige Details eines Angebots (alle Bilder, Beschreibung, Versand …)."""
    root = call("GetItem", (
        f"<ItemID>{escape(item_id)}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
        "<IncludeItemSpecifics>true</IncludeItemSpecifics>"
    ))
    return root.find("e:Item", NS)


def revise_price(item_id: str, new_price: float) -> None:
    call("ReviseInventoryStatus", (
        f"<InventoryStatus><ItemID>{escape(item_id)}</ItemID>"
        f"<StartPrice>{new_price:.2f}</StartPrice></InventoryStatus>"
    ))
