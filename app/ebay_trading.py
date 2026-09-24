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


# ── Einzelheiten für Bündel ─────────────────────────────────────────────

def item_details(item_id: str) -> dict:
    """Alles, was für ein Bündel-Angebot aus einem bestehenden Angebot gebraucht wird."""
    it = get_item(item_id)
    specifics = {}
    for nv in it.findall("e:ItemSpecifics/e:NameValueList", NS):
        name = _t(nv, "e:Name")
        if name:
            specifics[name] = [v.text for v in nv.findall("e:Value", NS) if v.text]
    return {
        **_parse_item(it),
        "pictures": [p.text for p in it.findall("e:PictureDetails/e:PictureURL", NS) if p.text],
        "category_id": _t(it, "e:PrimaryCategory/e:CategoryID"),
        "category_name": _t(it, "e:PrimaryCategory/e:CategoryName"),
        "condition_id": _t(it, "e:ConditionID"),
        "condition_name": _t(it, "e:ConditionDisplayName"),
        "description": _t(it, "e:Description") or "",
        "shipping_profile": _t(it, "e:SellerProfiles/e:SellerShippingProfile/e:ShippingProfileID"),
        "shipping_profile_name": _t(it, "e:SellerProfiles/e:SellerShippingProfile/e:ShippingProfileName"),
        "return_profile": _t(it, "e:SellerProfiles/e:SellerReturnProfile/e:ReturnProfileID"),
        "payment_profile": _t(it, "e:SellerProfiles/e:SellerPaymentProfile/e:PaymentProfileID"),
        "location": _t(it, "e:Location"),
        "postal_code": _t(it, "e:PostalCode"),
        "country": _t(it, "e:Country") or "DE",
        "specifics": specifics,
        "shipping_cost": _first_float(it, "e:ShippingDetails/e:ShippingServiceOptions/e:ShippingServiceCost"),
        "ean": _t(it, "e:ProductListingDetails/e:EAN"),
    }


def _first_float(el: ET.Element, path: str) -> float | None:
    v = _t(el, path)
    return float(v) if v else None


def build_item_xml(d: dict) -> str:
    """<Item>-Block für Add/VerifyAddFixedPriceItem aus einem Bündel-Entwurf."""
    specs = "".join(
        "<NameValueList><Name>{}</Name>{}</NameValueList>".format(
            escape(name), "".join(f"<Value>{escape(v)}</Value>" for v in values)
        )
        for name, values in d["specifics"].items() if values
    )
    pics = "".join(f"<PictureURL>{escape(u)}</PictureURL>" for u in d["pictures"][:24])
    parts = [
        "<Item>",
        f"<Title>{escape(d['title'][:80])}</Title>",
        f"<Description><![CDATA[{d['description']}]]></Description>",
        f"<PrimaryCategory><CategoryID>{escape(d['category_id'])}</CategoryID></PrimaryCategory>",
        f'<StartPrice currencyID="EUR">{d["price"]:.2f}</StartPrice>',
        f"<ConditionID>{escape(d['condition_id'])}</ConditionID>",
        f"<Country>{escape(d['country'])}</Country><Currency>EUR</Currency>",
    ]
    if d.get("location"):
        parts.append(f"<Location>{escape(d['location'])}</Location>")
    if d.get("postal_code"):
        parts.append(f"<PostalCode>{escape(d['postal_code'])}</PostalCode>")
    parts.append("<ListingDuration>GTC</ListingDuration><ListingType>FixedPriceItem</ListingType><Quantity>1</Quantity>")
    if d.get("sku"):
        parts.append(f"<SKU>{escape(d['sku'])}</SKU>")
    parts.append(f"<PictureDetails>{pics}</PictureDetails>")
    if specs:
        parts.append(f"<ItemSpecifics>{specs}</ItemSpecifics>")
    parts += [
        "<ProductListingDetails><EAN>Nicht zutreffend</EAN></ProductListingDetails>",
        "<SellerProfiles>",
        f"<SellerShippingProfile><ShippingProfileID>{escape(d['shipping_profile'])}</ShippingProfileID></SellerShippingProfile>",
        f"<SellerReturnProfile><ReturnProfileID>{escape(d['return_profile'])}</ReturnProfileID></SellerReturnProfile>",
        f"<SellerPaymentProfile><PaymentProfileID>{escape(d['payment_profile'])}</PaymentProfileID></SellerPaymentProfile>",
        "</SellerProfiles>",
        "<Site>Germany</Site>",
        "</Item>",
    ]
    return "".join(parts)


def _fees(root: ET.Element) -> list[tuple[str, float]]:
    out = []
    for f in root.findall("e:Fees/e:Fee", NS):
        amount = float(f.findtext("e:Fee", default="0", namespaces=NS))
        if amount:
            out.append((f.findtext("e:Name", namespaces=NS), amount))
    return out


def _warnings(root: ET.Element) -> list[str]:
    return [
        (e.findtext("e:LongMessage", namespaces=NS) or "").strip()
        for e in root.findall("e:Errors", NS)
        if e.findtext("e:SeverityCode", namespaces=NS) == "Warning"
    ]


def verify_listing(d: dict) -> dict:
    """Prüft ein Angebot bei eBay, ohne es anzulegen."""
    root = call("VerifyAddFixedPriceItem", build_item_xml(d))
    return {"fees": _fees(root), "warnings": _warnings(root)}


def add_listing(d: dict) -> dict:
    root = call("AddFixedPriceItem", build_item_xml(d))
    return {"item_id": root.findtext("e:ItemID", namespaces=NS), "fees": _fees(root), "warnings": _warnings(root)}


def end_listing(item_id: str) -> None:
    call("EndFixedPriceItem", f"<ItemID>{escape(item_id)}</ItemID><EndingReason>NotAvailable</EndingReason>")


def set_quantity(item_id: str, quantity: int) -> None:
    call("ReviseInventoryStatus", (
        f"<InventoryStatus><ItemID>{escape(item_id)}</ItemID><Quantity>{quantity}</Quantity></InventoryStatus>"
    ))


def relist(item_id: str) -> str:
    root = call("RelistFixedPriceItem", f"<Item><ItemID>{escape(item_id)}</ItemID></Item>")
    return root.findtext("e:ItemID", namespaces=NS)


def upload_picture(data: bytes, name: str = "collage.jpg") -> str:
    """Lädt ein Bild zu eBay hoch und liefert die dauerhafte Bild-Adresse."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<PictureName>{escape(name)}</PictureName><PictureSet>Supersize</PictureSet>"
        "</UploadSiteHostedPicturesRequest>"
    )
    r = httpx.post(
        f"{config.API_BASE}/ws/api.dll",
        headers={
            "X-EBAY-API-SITEID": config.SITE_ID,
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
            "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
            "X-EBAY-API-IAF-TOKEN": ebay_auth.access_token(),
        },
        files={
            "XML Payload": (None, xml, "text/xml"),
            "image": (name, data, "image/jpeg"),
        },
        timeout=120,
    )
    root = ET.fromstring(r.content)
    url = root.findtext("e:SiteHostedPictureDetails/e:FullURL", namespaces=NS)
    if not url:
        msg = root.findtext("e:Errors/e:LongMessage", namespaces=NS) or r.text[:300]
        raise EbayError(f"Bild-Upload fehlgeschlagen: {msg}")
    return url


def set_sku(item_id: str, sku: str) -> None:
    """Trägt eine SKU (Lagernummer) am Angebot ein – für Käufer unsichtbar."""
    call("ReviseFixedPriceItem", f"<Item><ItemID>{escape(item_id)}</ItemID><SKU>{escape(sku)}</SKU></Item>")
