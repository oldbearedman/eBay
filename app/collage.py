"""Erzeugt aus den Hauptbildern mehrerer Angebote eine Collage."""
import io
import math
import re

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

SIZE = 1600
GAP = 16
BG = (255, 255, 255)
BADGE = (0, 100, 210)


def _large(url: str) -> str:
    # eBay liefert je nach Adresse kleine Vorschaubilder – größte Variante (1600 px) anfordern
    url = re.sub(r"s-l\d+\.", "s-l1600.", url)
    return re.sub(r"\$_\d+\.", "$_57.", url)


def _fetch(url: str) -> Image.Image:
    r = httpx.get(_large(url), timeout=30, follow_redirects=True)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    return ImageOps.exif_transpose(img).convert("RGB")


def build(image_urls: list[str]) -> bytes:
    n = len(image_urls)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cell_w = (SIZE - GAP * (cols + 1)) // cols
    cell_h = (SIZE - GAP * (rows + 1)) // rows
    canvas = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(canvas)
    rad = max(28, min(cell_w, cell_h) // 14)
    font = ImageFont.load_default(size=int(rad * 1.2))
    for i, url in enumerate(image_urls):
        r, c = divmod(i, cols)
        # Letzte Zeile mittig ausrichten, wenn sie nicht voll ist
        in_row = cols if r < rows - 1 else n - cols * (rows - 1)
        offset = (cols - in_row) * (cell_w + GAP) // 2
        x = GAP + c * (cell_w + GAP) + offset
        y = GAP + r * (cell_h + GAP)
        img = _fetch(url)
        img = ImageOps.contain(img, (cell_w, cell_h), Image.LANCZOS)  # skaliert auch hoch
        canvas.paste(img, (x + (cell_w - img.width) // 2, y + (cell_h - img.height) // 2))
        # Nummern-Plakette oben links
        cx, cy = x + rad + 8, y + rad + 8
        draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=BADGE)
        draw.text((cx, cy), str(i + 1), fill="white", font=font, anchor="mm")
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90)
    return out.getvalue()
