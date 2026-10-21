"""Разбор чужого сайта (site_audit.inspect + ANALYZE_JS) в настоящем Chromium на локальных образцах.

Образцы повторяют то, на чём разбор ошибался вживую: шторка меню и бегущая строка Shopify с «0,00 €»,
перечёркнутые цены и цены за кг, логотип только в JSON-LD, блоки «появляются при прокрутке».
Без интернета; пропускается, если Chromium для Playwright не установлен.
"""

import functools
import http.server
import shutil
import struct
import threading
import zlib
from pathlib import Path

import pytest

from jarvis import site_audit
from jarvis.checks import check_browser
from jarvis.site_studio import hero_image

ROOT = Path(__file__).resolve().parent.parent
SHOPS = Path(__file__).parent / "fixtures" / "shops"
IMAGES = {
    "logo.png": (194, 66), "ld-logo.png": (300, 300), "truck.png": (40, 40), "visa-logo.png": (60, 40),
    "Fast_Alles_Banner_750x300.png": (750, 300), "Testshop_Logo_Social.png": (1200, 630), "hero.png": (1600, 700),
    "britva-mark.png": (240, 80), "salon.png": (1400, 600), "zerno-logo.png": (240, 80),
    **{f"p{i}.png": (600, 600) for i in range(1, 5)}, **{f"c{i}.png": (300, 300) for i in range(1, 5)},
}


def png(width: int, height: int) -> bytes:
    """Однотонная PNG нужного размера — чтобы не держать картинки в репозитории."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = (b"\x00" + b"\xc8\x50\x3c" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def shops(tmp_path_factory):
    root = tmp_path_factory.mktemp("shops")
    for page in SHOPS.glob("*.html"):
        shutil.copy(page, root / page.name)
    (root / "img").mkdir()
    for name, (w, h) in IMAGES.items():
        (root / "img" / name).write_bytes(png(w, h))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.handle_error = lambda *a: None  # браузер обрывает соединения — это не ошибка теста
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def analyze(shops, tmp_path_factory):
    from jarvis.config import Config
    if not check_browser(Config.load(ROOT / "config.toml"))[0].ok:
        pytest.skip("Chromium для Playwright не установлен")
    from jarvis.browser import BrowserController
    local = ROOT / ".playwright"
    shots = tmp_path_factory.mktemp("shots")
    browser = BrowserController(tmp_path_factory.mktemp("profile"), headless=True, channel="chromium",
                                timeout_ms=10000, browsers_path=local if local.exists() else None, screenshots_dir=shots)
    cache: dict[str, dict] = {}

    def run(page: str) -> dict:
        if page not in cache:
            found = site_audit.inspect(browser, f"{shops}/{page}", shots, page.split(".")[0], analyze=True)
            cache[page] = found["analysis"]
        return cache[page]
    yield run
    browser.close()


def pairs(analysis: dict) -> list[tuple[str, str]]:
    return [(p["name"], p["price"]) for p in analysis["products"]]


def test_shopify_cards_without_menus_struck_and_unit_prices(analyze):
    a = analyze("shopify.html")
    found = pairs(a)
    assert found[:3] == [("Protein Grießpudding", "2,99 €"),   # не «4.4 Normaler Preis», не 0,00 € и не 49,83 €/kg
                         ("Clear Whey Isolate", "24,90 €"),     # цена со скидкой, не перечёркнутая
                         ("Zerup Sirup", "5,99 €")]             # «39,93 € / kg» — цена за кг
    name, price = found[3]
    assert name.startswith("Probierbox mit sechs Sorten") and name.endswith("…") and len(name) <= 81
    assert price == "12,49 €"
    assert len(found) == 4  # нет шторки меню, окна поиска, скрытой вкладки и отзыва без цены
    assert [p["image"].rsplit("/", 1)[-1] for p in a["products"]] == ["p1.png", "p2.png", "p3.png", "p4.png"]


def test_shopify_logo_hero_and_notes(analyze):
    a = analyze("shopify.html")
    assert a["logo"].endswith("/img/logo.png")        # не баннер 750×300 из шторки меню
    assert hero_image(a).endswith("/img/hero.png")    # og:image — логотип
    notes = site_audit.business_notes(a)
    assert notes.startswith("Сайт: http") and "\nМеню: Aktion, Proben, Über uns\n" in notes
    products = next(line for line in notes.splitlines() if line.startswith("Товары и цены: "))
    assert "Kostenloser" not in products and "0,00" not in products and "Normaler" not in products


def test_services_price_list_without_product_links(analyze):
    a = analyze("services.html")
    assert pairs(a) == [("Мужская стрижка", "1 800 р."), ("Стрижка бороды", "от 900 ₽"),
                        ("Детская стрижка", "1200 руб.")]  # не «Горки-2 Рублёвское», не акция и не cookie
    assert a["logo"].endswith("/img/britva-mark.png")    # картинка-ссылка на главную в шапке
    assert hero_image(a).endswith("/img/salon.png")      # og:image в SVG — не фото


def test_woocommerce_sale_price_json_ld_and_reveal_on_scroll(analyze):
    a = analyze("woo.html")
    assert pairs(a) == [("Эфиопия Сидамо", "990 ₽"), ("Кения АА", "1 350 ₽"),
                        ("Бразилия Сантос", "750 ₽"),     # проявляется только после прокрутки
                        ("Колумбия Супремо", "890 ₽")]    # есть только в JSON-LD
    assert a["products"][3]["image"].endswith("/img/c1.png")
    assert a["logo"].endswith("/img/zerno-logo.png")    # логотип — SVG в коде, адрес картинки есть в JSON-LD
