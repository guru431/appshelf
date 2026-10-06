"""PWA: манифест и иконка экрана «Домой». Иконка — без прозрачности (apple-touch-icon iOS заливает её
чёрным) и с рисунком в безопасной зоне maskable (круг 80 %): Android обрезает её по своей маске."""
from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw

SIZES = (180, 192, 512)  # 180 — apple-touch-icon iPhone, 192 и 512 — манифест
BG, FG = (10, 132, 255), (255, 255, 255)
MANIFEST = {
    "name": "appshelf",
    "short_name": "appshelf",
    "lang": "ru",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#111111",
    "theme_color": "#111111",
    "icons": [{"src": f"/pwa/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any maskable"}
              for s in (192, 512)],
}


@lru_cache(maxsize=None)
def icon_png(size: int) -> bytes:
    """Три плитки приложений на полке. Рисуется вдвое крупнее 512 и сжимается — края сглажены."""
    k = 2
    img = Image.new("RGB", (512 * k, 512 * k), BG)
    d = ImageDraw.Draw(img)
    tile, gap, top = 92, 26, 184
    for i in range(3):
        x = 92 + i * (tile + gap)
        d.rounded_rectangle((x * k, top * k, (x + tile) * k, (top + tile) * k), radius=20 * k, fill=FG)
    d.rounded_rectangle((76 * k, 298 * k, 436 * k, 324 * k), radius=13 * k, fill=FG)
    out = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()
