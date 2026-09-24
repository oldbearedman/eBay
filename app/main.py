"""Weboberfläche des eBay Managers."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import advisor, ai, bundles, config, db, ebay_account, ebay_auth, ebay_orders, ebay_trading, market, promotions, settings, sync, traffic, wawi

log = logging.getLogger("ebay-manager")
templates = Jinja2Templates(directory=config.BASE_DIR / "app" / "templates")

SORTS = {
    "alter": "start_time ASC",
    "neu": "start_time DESC",
    "preis_ab": "price DESC",
    "preis_auf": "price ASC",
    "beobachter": "watch_count DESC",
    "titel": "title COLLATE NOCASE ASC",
    "markt": "start_time ASC",  # wird in Python nach Marktabstand sortiert
}


async def _auto_sync():
    while True:
        if ebay_auth.is_authorized():
            try:
                n = await asyncio.to_thread(sync.run_sync)
                log.info("Automatischer Abgleich: %d Angebote", n)
            except Exception:
                log.exception("Automatischer Abgleich fehlgeschlagen")
        await asyncio.sleep(config.SYNC_INTERVAL_HOURS * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    bundles.init()
    market.init()
    advisor.init()
    wawi.init()
    traffic.init()
    ebay_orders.init()
    task = asyncio.create_task(_auto_sync())
    yield
    task.cancel()


app = FastAPI(title="eBay Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "app" / "static"), name="static")


def _days_online(start_time: str | None) -> int | None:
    if not start_time:
        return None
    start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - start).days


@app.get("/", response_class=HTMLResponse)
def index(request: Request, sort: str = "alter", q: str = "", fehler: str = "", diag: str = ""):
    if not ebay_auth.is_authorized():
        return RedirectResponse("/anmelden", status_code=303)
    order = SORTS.get(sort, SORTS["alter"])
    with db.connect() as con:
        rows = con.execute(
            f"SELECT * FROM listings WHERE active = 1 AND title LIKE ? ORDER BY {order}",
            (f"%{q}%",),
        ).fetchall()
        last = con.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    checks = market.all_checks()
    diags = traffic.diagnose_all(checks)
    listings = [{**dict(r), "days": _days_online(r["start_time"]), "m": checks.get(r["item_id"]),
                 "dg": diags.get(r["item_id"])} for r in rows]
    diag_counts = {}
    for l in listings:
        if l["dg"]:
            diag_counts[l["dg"]["key"]] = diag_counts.get(l["dg"]["key"], 0) + 1
    if diag:
        listings = [l for l in listings if l["dg"] and l["dg"]["key"] == diag]
    if sort == "markt":
        listings.sort(key=lambda l: -(l["m"]["diff_pct"] if l["m"] and l["m"]["diff_pct"] is not None else -999))
    total = sum(l["price"] * max(l["quantity"], 1) for l in listings)
    return templates.TemplateResponse(request, "index.html", {
        "listings": listings, "sort": sort, "q": q, "last": last, "total": total, "fehler": fehler,
        "job": market.job, "diag": diag, "diag_counts": diag_counts, "diagnoses": traffic.DIAGNOSES,
    })


@app.post("/abgleichen")
async def do_sync():
    await asyncio.to_thread(sync.run_sync)
    return RedirectResponse("/", status_code=303)


@app.get("/anmelden", response_class=HTMLResponse)
def auth_page(request: Request, fehler: str = ""):
    expires = ebay_auth.refresh_expires_at()
    return templates.TemplateResponse(request, "auth.html", {
        "consent_url": ebay_auth.consent_url(),
        "authorized": ebay_auth.is_authorized(),
        "expires": datetime.fromtimestamp(expires).strftime("%d.%m.%Y") if expires else None,
        "fehler": fehler,
    })


@app.post("/anmelden")
async def auth_submit(adresse: str = Form(...)):
    try:
        ebay_auth.exchange_code(ebay_auth.extract_code(adresse))
    except Exception as exc:
        return RedirectResponse(f"/anmelden?fehler={str(exc)[:200]}", status_code=303)
    try:
        await asyncio.to_thread(sync.run_sync)
    except Exception:
        log.exception("Erster Abgleich fehlgeschlagen")
    return RedirectResponse("/", status_code=303)


# ── Bündel ──────────────────────────────────────────────────────────────

def _back(url: str, **msg) -> RedirectResponse:
    q = "&".join(f"{k}={quote(str(v))}" for k, v in msg.items() if v)
    return RedirectResponse(f"{url}?{q}" if q else url, status_code=303)


@app.post("/buendel")
async def bundle_create(request: Request):
    form = await request.form()
    ids = form.getlist("ids")
    if len(ids) < 2:
        return _back("/", fehler="Bitte mindestens zwei Angebote auswählen.")
    price = float(form["preis"]) if form.get("preis") else None
    try:
        bundle_id = await asyncio.to_thread(bundles.create_draft, ids, price)
    except Exception as exc:
        log.exception("Bündel-Entwurf fehlgeschlagen")
        return _back("/", fehler=str(exc))
    return RedirectResponse(f"/buendel/{bundle_id}", status_code=303)


@app.get("/buendel", response_class=HTMLResponse)
def bundle_list(request: Request, info: str = "", fehler: str = ""):
    return templates.TemplateResponse(request, "bundles.html", {
        "bundles": bundles.all_bundles(), "info": info, "fehler": fehler,
    })


@app.get("/buendel/{bundle_id}", response_class=HTMLResponse)
def bundle_edit(request: Request, bundle_id: int, info: str = "", fehler: str = "", pruefung: str = ""):
    b = bundles.get(bundle_id)
    if not b:
        raise HTTPException(404)
    try:
        profiles = ebay_account.shipping_profiles()
    except Exception:
        log.exception("Versandprofile nicht abrufbar")
        profiles = [{"id": b["draft"]["shipping_profile"], "name": "Versandprofil des ersten Artikels", "description": ""}]
    margin = wawi.bundle_margin([it["item_id"] for it in b["items"]], b["draft"]["price"], b["draft"].get("porto"))
    return templates.TemplateResponse(request, "bundle_edit.html", {
        "b": b, "d": b["draft"], "profiles": profiles, "margin": margin,
        "specifics_text": bundles.format_specifics(b["draft"]["specifics"]),
        "info": info, "fehler": fehler, "pruefung": pruefung, "ai_enabled": ai.enabled(),
    })


@app.get("/buendel/{bundle_id}/collage.jpg")
def bundle_collage(bundle_id: int):
    path = bundles.COLLAGE_DIR / f"{bundle_id}.jpg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/buendel/{bundle_id}")
async def bundle_action(request: Request, bundle_id: int):
    form = await request.form()
    action = form.get("aktion")
    url = f"/buendel/{bundle_id}"
    try:
        if action == "loeschen":
            bundles.delete_draft(bundle_id)
            return _back("/buendel", info="Entwurf gelöscht.")
        if action == "aufloesen":
            problems = await asyncio.to_thread(bundles.dissolve, bundle_id)
            await asyncio.to_thread(sync.run_sync)
            return _back(url, info="Bündel aufgelöst, Einzelartikel sind wieder online.",
                         fehler=" · ".join(problems))
        bundles.save_draft(bundle_id, dict(form))
        if action == "ki":
            await asyncio.to_thread(bundles.rewrite_text, bundle_id)
            return _back(url, info="Claude hat Titel und Beschreibung neu geschrieben.")
        if action == "pruefen":
            res = await asyncio.to_thread(bundles.verify, bundle_id)
            fees = sum(a for _, a in res["fees"])
            text = f"eBay hat das Angebot geprüft: alles in Ordnung. Voraussichtliche Gebühren: {fees:.2f} €".replace(".", ",")
            if res["warnings"]:
                text += " · Hinweise: " + " · ".join(res["warnings"])
            return _back(url, pruefung=text)
        if action == "einstellen":
            res = await asyncio.to_thread(bundles.publish, bundle_id)
            await asyncio.to_thread(sync.run_sync)
            return _back(url, info=f"Bündel ist online (#{res['item_id']}). Die Einzelartikel wurden herausgenommen.",
                         fehler=" · ".join(res["problems"]))
        return _back(url, info="Entwurf gespeichert.")
    except Exception as exc:
        log.exception("Bündel-Aktion %s fehlgeschlagen", action)
        return _back(url, fehler=str(exc))


# ── Marktwert ───────────────────────────────────────────────────────────

@app.post("/markt/alle")
async def market_all():
    if not market.job["running"]:
        asyncio.get_running_loop().run_in_executor(None, market.check_all)
    return JSONResponse(market.job)


@app.get("/markt/status")
def market_status():
    return JSONResponse(market.job)


@app.post("/markt/{item_id}")
async def market_check(item_id: str):
    try:
        return JSONResponse(await asyncio.to_thread(market.check, item_id))
    except Exception as exc:
        log.exception("Marktcheck fehlgeschlagen")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/markt/{item_id}", response_class=HTMLResponse)
def market_detail(request: Request, item_id: str, info: str = "", fehler: str = ""):
    with db.connect() as con:
        listing = con.execute("SELECT * FROM listings WHERE item_id = ?", (item_id,)).fetchone()
    if not listing:
        raise HTTPException(404)
    m = market.all_checks().get(item_id)
    w = wawi.for_items([item_id]).get(item_id)
    calc = None
    if w:
        ship = w["versand_kosten"]
        calc = {"now": wawi.profit(listing["price"], w["ek"], w["fee_rate"], ship)}
        if m and m.get("suggestion"):
            calc["suggestion"] = wawi.profit(m["suggestion"], w["ek"], w["fee_rate"], ship)
        if wawi.slow_items([item_id]):
            calc["floor"] = wawi.min_price(w["ek"], w["fee_rate"], ship, -settings.get("max_loss_per_bundle"))
    return templates.TemplateResponse(request, "market.html", {
        "l": dict(listing), "m": m, "w": w, "calc": calc, "info": info, "fehler": fehler,
        "dg": traffic.diagnose_all().get(item_id),
    })


@app.post("/markt/{item_id}/preis")
async def market_set_price(item_id: str, preis: str = Form(...)):
    url = f"/markt/{item_id}"
    try:
        new = round(float(preis.replace(",", ".")), 2)
        await asyncio.to_thread(ebay_trading.revise_price, item_id, new)
        with db.connect() as con:
            con.execute("UPDATE listings SET price = ? WHERE item_id = ?", (new, item_id))
        await asyncio.to_thread(market.check, item_id)
    except Exception as exc:
        return _back(url, fehler=str(exc))
    return _back(url, info=f"Preis bei eBay auf {new:.2f} € geändert.".replace(".", ","))


# ── Bündel-Vorschläge (Claude-Analyse) ──────────────────────────────────

@app.get("/vorschlaege", response_class=HTMLResponse)
def suggestions(request: Request, fehler: str = ""):
    with db.connect() as con:
        rows = con.execute("SELECT * FROM listings WHERE active = 1").fetchall()
    listings = {r["item_id"]: dict(r) for r in rows}
    fixed = [l for l in listings.values() if l["listing_type"] == "FixedPriceItem"]
    checks = market.all_checks()
    a = advisor.latest()
    if a and a.get("result"):
        for b in a["result"]["buendel"]:  # Marge immer mit aktuellen WaWi-Daten
            b["marge"] = wawi.bundle_margin(b["item_ids"], b["preis"])
    return templates.TemplateResponse(request, "suggestions.html", {
        "a": a, "running": advisor.state["running"], "listings": listings,
        "ai_enabled": ai.enabled(), "fehler": fehler,
        "total": len(fixed), "checked": sum(1 for l in fixed if l["item_id"] in checks),
    })


@app.post("/vorschlaege")
def suggestions_start():
    try:
        advisor.start()
    except Exception as exc:
        return _back("/vorschlaege", fehler=str(exc))
    return RedirectResponse("/vorschlaege", status_code=303)


@app.get("/vorschlaege/status")
def suggestions_status():
    return JSONResponse({"running": advisor.state["running"]})


# ── WaWi-Zuordnung ──────────────────────────────────────────────────────

@app.get("/zuordnung", response_class=HTMLResponse)
def links_page(request: Request, info: str = "", fehler: str = ""):
    prods = wawi.products() if wawi.available() else {}
    with db.connect() as con:
        listings = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM listings WHERE active = 1")}
        rows = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM wawi_links")}
    taken = {r["produktnr"] for r in rows.values() if r["confirmed"]}
    free = {k: v for k, v in prods.items() if k not in taken}
    suggestions, open_ = [], []
    for iid, l in listings.items():
        r = rows.get(iid)
        if r and r["confirmed"]:
            continue
        if r:
            cands = wawi.candidates(l["title"], free, n=8)
            if r["produktnr"] not in [c["produktnr"] for _, c in cands] and r["produktnr"] in free:
                cands.insert(0, (r["score"], free[r["produktnr"]]))
            suggestions.append({**l, "produktnr": r["produktnr"], "score": r["score"] or 0, "cands": cands,
                                "method": r["method"]})
        else:
            open_.append(l)
    suggestions.sort(key=lambda s: -s["score"])
    methods = {}
    for r in rows.values():
        if r["confirmed"] and r["item_id"] in listings:
            methods[r["method"]] = methods.get(r["method"], 0) + 1
    counts = {"sicher": sum(1 for r in rows.values() if r["confirmed"] and r["item_id"] in listings),
              "vorschlag": len(suggestions), "offen": len(open_)}
    return templates.TemplateResponse(request, "links.html", {
        "suggestions": suggestions, "open": open_, "counts": counts, "info": info, "fehler": fehler,
        "methods": methods, "missing_skus": len(wawi.missing_skus()), "ai_enabled": ai.enabled(),
    })


@app.post("/zuordnung/auto")
async def links_auto():
    stats = await asyncio.to_thread(wawi.auto_link)
    return _back("/zuordnung", info=(
        f"Zugeordnet: {stats['sku']} über SKU, {stats['eindeutig']} eindeutig über den Titel, "
        f"{stats['titel']} Vorschläge zum Prüfen, {stats['offen']} ohne Treffer."))


@app.post("/zuordnung/bestaetigen")
async def links_confirm(request: Request):
    form = await request.form()
    chosen = set(form.getlist("ok"))
    if form.get("min_score"):
        with db.connect() as con:
            chosen |= {r["item_id"] for r in con.execute(
                "SELECT item_id FROM wawi_links WHERE confirmed = 0 AND score >= ?", (float(form["min_score"]),))}
    with db.connect() as con:
        taken = {r["produktnr"]: r["item_id"] for r in con.execute(
            "SELECT item_id, produktnr FROM wawi_links WHERE confirmed = 1")}
    done, conflicts = 0, []
    for iid in chosen:
        pnr = form.get(f"p_{iid}")
        if not pnr:
            with db.connect() as con:
                r = con.execute("SELECT produktnr FROM wawi_links WHERE item_id = ?", (iid,)).fetchone()
            pnr = r["produktnr"] if r else None
        if not pnr:
            continue
        if pnr in taken and taken[pnr] != iid:
            conflicts.append(pnr)
            continue
        wawi.link(iid, pnr)
        taken[pnr] = iid
        done += 1
    return _back("/zuordnung", info=f"{done} Zuordnungen bestätigt.",
                 fehler=(f"{len(conflicts)} übersprungen – der WaWi-Artikel ist schon einem anderen Angebot zugeordnet: "
                         + ", ".join(conflicts)) if conflicts else "")


# ── Einstellungen ───────────────────────────────────────────────────────

@app.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, info: str = ""):
    return templates.TemplateResponse(request, "settings.html", {
        "values": settings.all_values(), "labels": settings.LABELS, "info": info,
    })


@app.post("/einstellungen")
async def settings_save(request: Request):
    form = await request.form()
    for key in settings.DEFAULTS:
        if form.get(key):
            settings.set_value(key, float(str(form[key]).replace(",", ".")))
    return _back("/einstellungen", info="Gespeichert.")



@app.post("/zuordnung/claude")
async def links_claude():
    from . import link_ai
    try:
        s = await asyncio.to_thread(link_ai.run)
    except Exception as exc:
        log.exception("Claude-Zuordnung fehlgeschlagen")
        return _back("/zuordnung", fehler=str(exc))
    return _back("/zuordnung", info=(
        f"Claude: {s['sicher']} sicher zugeordnet, {s['vorschlag']} Vorschläge zum Prüfen, "
        f"{s['keiner']} ohne passenden WaWi-Artikel."))


@app.post("/zuordnung/sku")
async def links_sku():
    r = await asyncio.to_thread(wawi.write_skus)
    return _back("/zuordnung", info=f"SKU bei {r['done']} Angeboten eingetragen.", fehler=" · ".join(r["errors"][:5]))


# ── Kombi-Rabatt ────────────────────────────────────────────────────────

@app.get("/rabatte", response_class=HTMLResponse)
def promo_page(request: Request, q: str = "", nur: str = "", qty: int = 2, pct: float = 15,
               info: str = "", fehler: str = ""):
    try:
        promos = promotions.list_all()
    except Exception as exc:
        promos, fehler = [], fehler or str(exc)
    preview = None
    if "q" in request.query_params:
        with db.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM listings WHERE active = 1 AND listing_type = 'FixedPriceItem' AND title LIKE ? ORDER BY title",
                (f"%{q}%",))]
        if nur == "ladenhueter":
            slow = set(wawi.slow_items([r["item_id"] for r in rows]))
            rows = [r for r in rows if r["item_id"] in slow]
        elif nur in ("unsichtbar", "kauf"):
            dg = traffic.diagnose_all()
            rows = [r for r in rows if dg.get(r["item_id"], {}).get("key") == nur]
        margins = promotions.margin_check([r["item_id"] for r in rows], pct) if rows else {}
        preview = [{**r, "m": margins.get(r["item_id"], {"level": "unbekannt"})} for r in rows]
    return templates.TemplateResponse(request, "promotions.html", {
        "promos": promos, "preview": preview, "q": q, "nur": nur, "qty": qty, "pct": pct,
        "info": info, "fehler": fehler,
    })


@app.post("/rabatte")
async def promo_create(request: Request):
    form = await request.form()
    ids = form.getlist("ids")
    if len(ids) < 2:
        return _back("/rabatte", fehler="Bitte mindestens zwei Artikel auswählen.")
    with db.connect() as con:
        img = con.execute("SELECT image_url FROM listings WHERE item_id = ?", (ids[0],)).fetchone()["image_url"]
    draft = form.get("modus") == "entwurf"
    try:
        await asyncio.to_thread(
            promotions.create_order_discount, form["name"], form["description"], ids,
            int(form["qty"]), float(form["pct"]), int(form["days"]), img, draft)
    except Exception as exc:
        return _back("/rabatte", fehler=str(exc))
    return _back("/rabatte", info=(f"Entwurf mit {len(ids)} Artikeln angelegt." if draft
                                   else f"Kombi-Rabatt für {len(ids)} Artikel gestartet (aktiv in ca. 5 Minuten)."))


@app.post("/rabatte/aktion")
async def promo_action(id: str = Form(...), was: str = Form(...)):
    fn = {"pause": promotions.pause, "resume": promotions.resume, "delete": promotions.delete}[was]
    try:
        await asyncio.to_thread(fn, id)
    except Exception as exc:
        return _back("/rabatte", fehler=str(exc))
    return _back("/rabatte", info={"pause": "Aktion pausiert.", "resume": "Aktion läuft wieder.", "delete": "Aktion gelöscht."}[was])
