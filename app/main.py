"""Weboberfläche des eBay Managers."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, ebay_auth, sync

log = logging.getLogger("ebay-manager")
templates = Jinja2Templates(directory=config.BASE_DIR / "app" / "templates")

SORTS = {
    "alter": "start_time ASC",
    "neu": "start_time DESC",
    "preis_ab": "price DESC",
    "preis_auf": "price ASC",
    "beobachter": "watch_count DESC",
    "titel": "title COLLATE NOCASE ASC",
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
def index(request: Request, sort: str = "alter", q: str = ""):
    if not ebay_auth.is_authorized():
        return RedirectResponse("/anmelden", status_code=303)
    order = SORTS.get(sort, SORTS["alter"])
    with db.connect() as con:
        rows = con.execute(
            f"SELECT * FROM listings WHERE active = 1 AND title LIKE ? ORDER BY {order}",
            (f"%{q}%",),
        ).fetchall()
        last = con.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    listings = [{**dict(r), "days": _days_online(r["start_time"])} for r in rows]
    total = sum(l["price"] * max(l["quantity"], 1) for l in listings)
    return templates.TemplateResponse(request, "index.html", {
        "listings": listings, "sort": sort, "q": q, "last": last, "total": total,
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
