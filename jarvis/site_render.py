"""Вёрстка сайта-концепта из описания (spec) — один самодостаточный index.html.

Тексты и стиль придумывает модель (см. site_studio.py), а вёрстку делает этот модуль:
так сайт получается аккуратным даже с маленькой локальной моделью, проходит проверку
качества (SEO, мобильная версия, доступность) и не содержит кода от модели.

Всё, что пришло из spec, экранируется; адреса картинок — только http(s); цвета — только HEX.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
from typing import Any

# Направления дизайна: шрифты (Google Fonts с кириллицей + системный запасной вариант) и характер.
DIRECTIONS: dict[str, dict[str, Any]] = {
    "bold": {  # спорт, энергия, еда, молодёжные бренды — как в ролике: жирный курсив капсом
        "fonts": "family=Montserrat:ital,wght@0,700;1,800;1,900&family=Inter:wght@400;500;600;700",
        "head": "'Montserrat', 'Arial Black', 'Segoe UI Black', Impact, sans-serif",
        "body": "'Inter', 'Segoe UI', system-ui, sans-serif",
        "weight": 900, "italic": True, "upper": True, "radius": 18, "tracking": "-.02em", "leading": .9,
    },
    "elegant": {  # красота, мода, рестораны, премиум
        "fonts": "family=Cormorant+Garamond:ital,wght@0,500;0,600;1,500&family=Manrope:wght@400;500;600;700",
        "head": "'Cormorant Garamond', Georgia, 'Times New Roman', serif",
        "body": "'Manrope', 'Segoe UI', system-ui, sans-serif",
        "weight": 600, "italic": False, "upper": False, "radius": 4, "tracking": "-.01em", "leading": 1.0,
    },
    "tech": {  # IT, сервисы, B2B
        "fonts": "family=Unbounded:wght@500;700;800&family=Inter:wght@400;500;600;700",
        "head": "'Unbounded', 'Segoe UI', system-ui, sans-serif",
        "body": "'Inter', 'Segoe UI', system-ui, sans-serif",
        "weight": 700, "italic": False, "upper": False, "radius": 14, "tracking": "-.03em", "leading": 1.02,
    },
    "warm": {  # кафе, пекарни, ремесло, местный бизнес
        "fonts": "family=Lora:ital,wght@0,600;0,700;1,600&family=Nunito:wght@400;600;700;800",
        "head": "'Lora', Georgia, serif",
        "body": "'Nunito', 'Segoe UI', system-ui, sans-serif",
        "weight": 700, "italic": False, "upper": False, "radius": 24, "tracking": "-.01em", "leading": 1.05,
    },
    "playful": {  # дети, животные, сладости, развлечения
        "fonts": "family=Comfortaa:wght@600;700&family=Nunito:wght@400;600;700;800",
        "head": "'Comfortaa', 'Trebuchet MS', sans-serif",
        "body": "'Nunito', 'Segoe UI', system-ui, sans-serif",
        "weight": 700, "italic": False, "upper": False, "radius": 30, "tracking": "-.02em", "leading": 1.05,
    },
}
DEFAULT_DIRECTION = "bold"

# Значки преимуществ: простые линии 24×24 (по кругу).
ICONS = (
    '<path d="M5 12.5 10 17.5 19 7"/>',
    '<path d="M13 3 5 14h6l-1 7 8-11h-6l1-7Z"/>',
    '<path d="M12 3 14.6 8.6 20.8 9.3 16.2 13.5 17.5 19.6 12 16.5 6.5 19.6 7.8 13.5 3.2 9.3 9.4 8.6Z"/>',
    '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    '<path d="M12 21s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 5.6-7 10-7 10Z"/>',
    '<path d="M4 12h16M12 4v16"/><circle cx="12" cy="12" r="8.5"/>',
)

PRICE_RE = re.compile(r"\d[\d\s .,]*")


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def safe_url(url: Any) -> str:
    url = str(url or "").strip()
    return url if re.match(r"^https?://[^\s\"'<>]+$", url, re.I) else ""


# --- цвета ---------------------------------------------------------------
def hex_color(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    m = re.fullmatch(r"#?([0-9a-f]{3}|[0-9a-f]{6})", text)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in (r, g, b))


def luminance(color: str) -> float:
    def ch(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(color)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """t=0 — цвет a, t=1 — цвет b."""
    ra, rb = rgb(a), rgb(b)
    return to_hex(*(x + (y - x) * t for x, y in zip(ra, rb)))


def readable(color: str, background: str, ratio: float = 4.5) -> str:
    """Тот же оттенок, затемнённый или осветлённый до нужного контраста с фоном."""
    target = "#000000" if luminance(background) > 0.4 else "#ffffff"
    for i in range(21):
        candidate = mix(color, target, i / 20)
        if contrast(candidate, background) >= ratio:
            return candidate
    return target


def on_color(background: str) -> str:
    """Текст на заливке: белый или почти чёрный — что контрастнее."""
    return "#ffffff" if contrast("#ffffff", background) >= contrast("#111111", background) else "#111111"


def palette(style: dict[str, Any]) -> dict[str, str]:
    primary = hex_color(style.get("primary")) or "#e5262f"
    accent = hex_color(style.get("accent")) or mix(primary, "#ffffff", .75)
    dark = str(style.get("background", "light")).lower() == "dark"
    if dark:
        bg, text, muted = mix(primary, "#0b0b0d", .93), "#f5f5f6", "#b7b7be"
        surface, line = mix(primary, "#0b0b0d", .86), mix(primary, "#0b0b0d", .74)
    else:
        bg, text, muted = mix(primary, "#ffffff", .965), "#141416", "#56565d"
        surface, line = mix(primary, "#ffffff", .9), mix(primary, "#ffffff", .78)
    # Кнопки — фирменным цветом; если он почти совпадает с фоном (белый бренд), берём текст.
    fill = primary if contrast(primary, bg) >= 1.6 else text
    return {
        "primary": fill, "on-primary": on_color(fill), "accent": accent, "on-accent": on_color(accent),
        "ink": readable(primary, bg), "bg": bg, "surface": surface, "line": line, "text": text,
        "muted": readable(muted, bg), "band": fill, "on-band": on_color(fill),
        "accent-soft": mix(accent, bg, .55), "scheme": "dark" if dark else "light",
    }


# --- цены для корзины ---------------------------------------------------------------
def parse_price(text: Any) -> tuple[float | None, str]:
    """«2 490 ₽» → (2490.0, "₽"); «от 1500 руб.» → (1500.0, "₽"); без числа — (None, "")."""
    raw = str(text or "")
    m = PRICE_RE.search(raw)
    if not m:
        return None, ""
    digits = m.group(0).replace(" ", "").replace(" ", "").rstrip(".,")
    if digits.count(",") == 1 and digits.count(".") == 0 and len(digits.split(",")[1]) <= 2:
        digits = digits.replace(",", ".")
    digits = digits.replace(",", "")
    if digits.count(".") > 1 or (digits.count(".") == 1 and len(digits.split(".")[1]) == 3):
        digits = digits.replace(".", "")  # «1.490 €» — разделитель тысяч
    try:
        value = float(digits)
    except ValueError:
        return None, ""
    low = raw.lower()
    currency = ("₽" if ("₽" in raw or "руб" in low or re.search(r"\bр\.?(?:\s|$)", low)) else
                "€" if ("€" in raw or "eur" in low) else "$" if ("$" in raw or "usd" in low) else
                "₸" if ("₸" in raw or "тг" in low) else "")
    return value, currency


# --- разметка ---------------------------------------------------------------
def initials(name: str) -> str:
    words = [w for w in re.findall(r"[\w’']+", name or "") if w]
    if not words:
        return "★"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def _hero_title(title: str) -> str:
    # Слова появляются по очереди (анимация в CSS, без JS).
    words = (title or "").split()
    return " ".join(f'<span class="w" style="--i:{i}">{esc(w)}</span>' for i, w in enumerate(words))


def _icon(i: int) -> str:
    return (f'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICONS[i % len(ICONS)]}</svg>')


def _product_card(i: int, p: dict[str, Any], cart: bool) -> str:
    image = safe_url(p.get("image"))
    media = (f'<img src="{esc(image)}" alt="{esc(p.get("name"))}" loading="lazy" referrerpolicy="no-referrer" '
             'onerror="this.parentNode.classList.add(\'noimg\');this.remove()">' if image else "")
    tag = f'<span class="tag">{esc(p["tag"])}</span>' if p.get("tag") else ""
    price = f'<p class="price">{esc(p["price"])}</p>' if p.get("price") else ""
    if cart:
        button = (f'<button type="button" class="btn small add" data-add="{i}" '
                  f'aria-label="Добавить в корзину: {esc(p.get("name"))}">В корзину</button>')
    else:
        button = (f'<a class="btn small ghost-btn" href="#contact" data-pick="{esc(p.get("name"))}">'
                  f'Заказать<span class="sr"> {esc(p.get("name"))}</span></a>')
    return (f'<article class="card" data-reveal style="--d:{i % 3}">'
            f'<div class="ph{"" if image else " noimg"}" data-initials="{esc(initials(p.get("name", "")))}">{media}{tag}</div>'
            f'<div class="card-body"><h3>{esc(p.get("name"))}</h3>'
            f'<p class="muted">{esc(p.get("text"))}</p>'
            f'<div class="card-foot">{price}{button}</div></div></article>')


def render(spec: dict[str, Any]) -> str:
    """spec (уже проверенный site_studio.normalize_spec) → полный HTML-документ."""
    style = spec.get("style") or {}
    direction = DIRECTIONS.get(str(style.get("direction")), DIRECTIONS[DEFAULT_DIRECTION])
    colors = palette(style)
    brand = spec.get("brand") or "Бренд"
    hero = spec.get("hero") or {}
    products = list(spec.get("products") or [])
    priced = [p for p in products if parse_price(p.get("price"))[0] is not None]
    cart = len(products) >= 2 and len(priced) == len(products)
    contacts = spec.get("contacts") or {}
    images = spec.get("images") or {}
    year = _dt.date.today().year
    lang = re.sub(r"[^a-zA-Z-]", "", str(spec.get("lang") or "ru"))[:8] or "ru"

    tokens = "".join(f"--{k}:{v};" for k, v in colors.items() if k != "scheme")
    root = (f":root{{{tokens}--head:{direction['head']};--body:{direction['body']};"
            f"--hw:{direction['weight']};--hs:{'italic' if direction['italic'] else 'normal'};"
            f"--ht:{'uppercase' if direction['upper'] else 'none'};--r:{direction['radius']}px;"
            f"--track:{direction['tracking']};--lead:{direction['leading']};color-scheme:{colors['scheme']}}}")

    nav_items = []
    if products:
        nav_items.append(("catalog", spec.get("products_title") or "Каталог"))
    if spec.get("benefits"):
        nav_items.append(("why", "Почему мы"))
    if spec.get("steps"):
        nav_items.append(("how", "Как это работает"))
    if spec.get("faq"):
        nav_items.append(("faq", "Вопросы"))
    nav_items.append(("contact", "Контакты"))
    nav = "".join(f'<a href="#{a}">{esc(t)}</a>' for a, t in nav_items)

    logo_url = safe_url(images.get("logo"))
    logo = (f'<img src="{esc(logo_url)}" alt="{esc(brand)}" referrerpolicy="no-referrer" '
            f'onerror="this.replaceWith(document.createTextNode(this.alt))">' if logo_url else esc(brand))

    hero_img = safe_url(images.get("hero"))
    visual_inner = (f'<img src="{esc(hero_img)}" alt="" referrerpolicy="no-referrer" '
                    'onerror="this.remove()">' if hero_img else "")
    visual = (f'<div class="visual" aria-hidden="true"><div class="ring r1"></div><div class="ring r2"></div>'
              f'<div class="orb" data-initials="{esc(initials(brand))}">{visual_inner}</div></div>')

    cta2 = (f'<a class="btn ghost-btn" href="#{"catalog" if products else "how"}">{esc(hero.get("cta2"))}</a>'
            if hero.get("cta2") else "")
    parts: list[str] = []
    parts.append(
        f'<header class="top"><div class="wrap top-in"><a class="logo" href="#top">{logo}</a>'
        f'<nav class="nav" id="nav" aria-label="Разделы">{nav}</nav>'
        '<div class="top-acts">'
        + ('<button type="button" class="cart-btn" id="cartBtn" aria-controls="cart" aria-expanded="false">'
           '<span class="cart-label">Корзина</span> <span class="count" id="cartCount">0</span></button>' if cart else
           '<a class="btn small" href="#contact">Связаться</a>')
        + '<button type="button" class="menu-btn" id="menuBtn" aria-controls="nav" aria-expanded="false" '
          'aria-label="Меню"><span></span><span></span></button></div></div></header>')
    parts.append(
        f'<main id="top"><section class="hero"><div class="ghost" aria-hidden="true">{esc(brand)}</div>'
        f'<div class="wrap hero-in"><div class="hero-text">'
        + (f'<p class="kicker">{esc(hero.get("kicker"))}</p>' if hero.get("kicker") else "")
        + f'<h1>{_hero_title(hero.get("title") or brand)}</h1>'
        f'<p class="lead">{esc(hero.get("subtitle"))}</p>'
        f'<div class="acts"><a class="btn" href="#{"catalog" if products else "contact"}">'
        f'{esc(hero.get("cta") or "Подробнее")}</a>{cta2}</div></div>{visual}</div></section>')

    ticker = [t for t in (spec.get("ticker") or []) if t]
    if ticker:
        line = "".join(f"<span>{esc(t)}</span><i>✦</i>" for t in ticker)
        # Бегущая строка — украшение (те же слова есть в разделах), читалке экрана не нужна.
        parts.append(f'<div class="ticker-wrap" aria-hidden="true"><div class="ticker"><div class="track">'
                     f'{line}{line}{line}{line}</div></div></div>')

    if products:
        cards = "".join(_product_card(i, p, cart) for i, p in enumerate(products))
        parts.append(f'<section class="sec" id="catalog"><div class="wrap"><div class="sec-head" data-reveal>'
                     f'<h2>{esc(spec.get("products_title") or "Каталог")}</h2>'
                     + (f'<p class="muted">{esc(spec.get("products_text"))}</p>' if spec.get("products_text") else "")
                     + f'</div><div class="grid cards">{cards}</div></div></section>')

    highlight = spec.get("highlight") or {}
    if highlight.get("title"):
        parts.append(f'<section class="band"><div class="wrap band-in" data-reveal>'
                     f'<p class="band-title">{esc(highlight["title"])}</p>'
                     + (f'<p class="band-text">{esc(highlight.get("text"))}</p>' if highlight.get("text") else "")
                     + '</div></section>')

    if spec.get("benefits"):
        items = "".join(f'<li class="benefit" data-reveal style="--d:{i % 4}">{_icon(i)}<h3>{esc(b.get("title"))}</h3>'
                        f'<p class="muted">{esc(b.get("text"))}</p></li>' for i, b in enumerate(spec["benefits"]))
        parts.append(f'<section class="sec" id="why"><div class="wrap"><div class="sec-head" data-reveal>'
                     f'<h2>{esc(spec.get("benefits_title") or "Почему мы")}</h2></div>'
                     f'<ul class="grid benefits">{items}</ul></div></section>')

    if spec.get("steps"):
        items = "".join(f'<li class="step" data-reveal style="--d:{i}"><span class="num">{i + 1:02d}</span>'
                        f'<h3>{esc(s.get("title"))}</h3><p class="muted">{esc(s.get("text"))}</p></li>'
                        for i, s in enumerate(spec["steps"]))
        parts.append(f'<section class="sec alt" id="how"><div class="wrap"><div class="sec-head" data-reveal>'
                     f'<h2>{esc(spec.get("steps_title") or "Как это работает")}</h2></div>'
                     f'<ol class="grid steps">{items}</ol></div></section>')

    if spec.get("faq"):
        items = "".join(f'<details data-reveal><summary>{esc(f.get("q"))}</summary><p>{esc(f.get("a"))}</p></details>'
                        for f in spec["faq"])
        parts.append(f'<section class="sec" id="faq"><div class="wrap narrow"><div class="sec-head" data-reveal>'
                     f'<h2>Вопросы и ответы</h2></div><div class="faq">{items}</div></div></section>')

    cta = spec.get("cta") or {}
    info = []
    phone = str(contacts.get("phone") or "").strip()
    email = str(contacts.get("email") or "").strip()
    if phone:
        info.append(f'<li><span>Телефон</span><a href="tel:{esc(re.sub(r"[^0-9+]", "", phone))}">{esc(phone)}</a></li>')
    if email:
        info.append(f'<li><span>Почта</span><a href="mailto:{esc(email)}">{esc(email)}</a></li>')
    if contacts.get("address"):
        info.append(f'<li><span>Адрес</span><p>{esc(contacts["address"])}</p></li>')
    if contacts.get("hours"):
        info.append(f'<li><span>Часы работы</span><p>{esc(contacts["hours"])}</p></li>')
    contact_list = f'<ul class="contacts">{"".join(info)}</ul>' if info else ""
    parts.append(
        f'<section class="sec cta" id="contact"><div class="wrap cta-in"><div data-reveal>'
        f'<h2>{esc(cta.get("title") or "Свяжитесь с нами")}</h2>'
        f'<p class="lead">{esc(cta.get("text"))}</p>{contact_list}</div>'
        f'<form class="form" id="leadForm" data-email="{esc(email)}" data-brand="{esc(brand)}" data-reveal>'
        f'<label for="fName">Имя</label><input id="fName" name="name" autocomplete="name" required>'
        f'<label for="fContact">Телефон или почта</label><input id="fContact" name="contact" autocomplete="tel" required>'
        f'<label for="fMsg">Сообщение</label><textarea id="fMsg" name="message" rows="4"></textarea>'
        f'<button class="btn" type="submit">{esc(cta.get("button") or "Отправить заявку")}</button>'
        f'<p class="form-note" id="formNote" role="status"></p></form></div></section></main>')
    parts.append(f'<footer class="foot"><div class="wrap foot-in"><span>© {year} {esc(brand)}</span>'
                 f'<a href="#top">Наверх ↑</a></div></footer>')
    if cart:
        parts.append('<div class="scrim" id="scrim" hidden></div>'
                     '<aside class="cart" id="cart" aria-label="Корзина" hidden><div class="cart-head">'
                     '<h2>Корзина</h2><button type="button" class="x" id="cartClose" aria-label="Закрыть корзину">×</button></div>'
                     '<ul class="cart-list" id="cartList"></ul><p class="cart-empty" id="cartEmpty">Пока пусто.</p>'
                     '<div class="cart-foot"><p>Итого: <b id="cartTotal">0</b></p>'
                     '<button type="button" class="btn" id="checkout">Оформить заказ</button></div></aside>')
        items = [{"name": p.get("name", ""), "price": parse_price(p.get("price"))[0],
                  "cur": parse_price(p.get("price"))[1]} for p in products]
        data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
        parts.append(f'<script type="application/json" id="products-data">{data}</script>')

    description = spec.get("description") or hero.get("subtitle") or brand
    title = spec.get("page_title") or (f"{brand} — {hero.get('title')}" if hero.get("title") else brand)
    fonts = f'https://fonts.googleapis.com/css2?{direction["fonts"]}&display=swap'
    return (
        f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{esc(title)[:120]}</title>\n<meta name="description" content="{esc(description)[:300]}">\n'
        f'<meta property="og:title" content="{esc(title)[:120]}">\n'
        f'<meta property="og:description" content="{esc(description)[:300]}">\n'
        + (f'<meta property="og:image" content="{esc(hero_img)}">\n' if hero_img else "")
        + f'<meta name="theme-color" content="{colors["primary"]}">\n'
        f'<meta name="generator" content="J.A.R.V.I.S. site studio">\n'
        f'<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        f'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        f'<link rel="stylesheet" href="{esc(fonts)}">\n'
        f'<script>document.documentElement.classList.add("js")</script>\n'
        f'<style>{root}{CSS}</style>\n</head>\n<body>\n'
        + "\n".join(parts)
        + f'\n<script>{JS}</script>\n</body>\n</html>\n')


CSS = r"""
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:17px/1.6 var(--body);overflow-x:hidden}
img{max-width:100%;display:block}
a{color:var(--ink)}
h1,h2,h3,.band-title,.logo{font-family:var(--head);font-weight:var(--hw);font-style:var(--hs);text-transform:var(--ht);letter-spacing:var(--track);line-height:var(--lead)}
h2{font-size:clamp(34px,5vw,64px);margin:0 0 14px}
h3{font-size:clamp(19px,1.6vw,23px);line-height:1.15;margin:0 0 8px}
p{margin:0 0 12px}
.muted{color:var(--muted)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.wrap{width:min(1200px,100% - 40px);margin-inline:auto}
.narrow{width:min(860px,100% - 40px)}
:focus-visible{outline:3px solid var(--ink);outline-offset:3px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:48px;padding:12px 26px;border-radius:var(--r);border:2px solid var(--primary);background:var(--primary);color:var(--on-primary);font:700 16px/1.1 var(--body);text-decoration:none;cursor:pointer;transition:transform .25s,box-shadow .25s,background .25s}
.btn:hover{transform:translateY(-2px);box-shadow:0 12px 28px -12px var(--primary)}
.btn.small{min-height:42px;padding:9px 18px;font-size:15px}
.ghost-btn{background:transparent;color:var(--ink);border-color:var(--line)}
.ghost-btn:hover{border-color:var(--ink);box-shadow:none}
.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 86%,transparent);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.top-in{display:flex;align-items:center;gap:24px;min-height:70px}
.logo{font-size:24px;text-decoration:none;color:var(--text);white-space:nowrap;line-height:1.1;min-width:0;overflow:hidden;text-overflow:ellipsis;padding:4px 0}
.logo img{height:38px;width:auto}
.nav{display:flex;gap:26px;margin-left:auto}
.nav a{color:var(--text);text-decoration:none;font-weight:600;font-size:15px;opacity:.82;padding:10px 0}
.nav a:hover{opacity:1;color:var(--ink)}
.top-acts{display:flex;align-items:center;gap:10px}
.cart-btn{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:8px 16px;border-radius:999px;border:1.5px solid var(--line);background:var(--surface);color:var(--text);font:600 15px var(--body);cursor:pointer}
.count{min-width:24px;height:24px;padding:0 6px;border-radius:12px;display:grid;place-items:center;background:var(--primary);color:var(--on-primary);font-size:13px}
.count.bump{animation:bump .4s}
.menu-btn{display:none;width:46px;height:46px;border-radius:12px;border:1.5px solid var(--line);background:none;cursor:pointer;flex-direction:column;justify-content:center;align-items:center;gap:6px}
.menu-btn span{display:block;width:18px;height:2px;background:var(--text);border-radius:1px}
.hero{position:relative;overflow:hidden;padding:clamp(48px,8vw,110px) 0 clamp(56px,8vw,96px)}
.ghost{position:absolute;left:50%;top:52%;translate:-50% -50%;font:var(--hw) var(--hs) clamp(120px,24vw,360px)/1 var(--head);text-transform:uppercase;white-space:nowrap;color:transparent;-webkit-text-stroke:1.5px var(--line);opacity:.7;pointer-events:none;user-select:none}
.hero-in{position:relative;display:grid;grid-template-columns:1.15fr .85fr;gap:40px;align-items:center}
.kicker{display:inline-block;font:700 13px/1 var(--body);letter-spacing:.16em;text-transform:uppercase;color:var(--ink);padding:9px 14px;border-radius:999px;background:var(--accent-soft);margin-bottom:22px}
h1{font-size:clamp(46px,7.2vw,108px);margin:0 0 22px;text-wrap:balance}
h1 .w{display:inline-block;animation:rise .8s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--i) * 90ms + 120ms)}
h1 .w:nth-child(even){color:var(--ink)}
.lead{font-size:clamp(18px,1.6vw,21px);max-width:36em;color:var(--muted)}
.acts{display:flex;flex-wrap:wrap;gap:12px;margin-top:28px}
.visual{position:relative;aspect-ratio:1;width:min(100%,460px);justify-self:center}
.ring{position:absolute;inset:0;border-radius:50%;border:2px dashed var(--line)}
.ring.r1{animation:spin 40s linear infinite}
.ring.r2{inset:9%;border:2px solid transparent;border-top-color:var(--primary);border-right-color:var(--primary);animation:spin 7s linear infinite}
.orb{position:absolute;inset:17%;border-radius:50%;overflow:hidden;display:grid;place-items:center;background:radial-gradient(circle at 34% 30%,var(--accent),var(--primary) 62%,color-mix(in srgb,var(--primary) 70%,#000));box-shadow:0 30px 80px -30px var(--primary);animation:float 6s ease-in-out infinite}
.orb::after{content:attr(data-initials);font:var(--hw) var(--hs) clamp(48px,8vw,96px)/1 var(--head);color:var(--on-primary);opacity:.92}
.orb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:1}
.ticker-wrap{overflow:hidden;padding:14px 0}
.ticker{background:var(--band);color:var(--on-band);overflow:hidden;padding:18px 0;transform:rotate(-1.2deg);width:calc(100% + 40px);margin-left:-20px}
.track{display:flex;gap:28px;width:max-content;animation:marquee 32s linear infinite;font:var(--hw) var(--hs) clamp(22px,2.6vw,34px)/1 var(--head);text-transform:uppercase;white-space:nowrap}
.track i{font-style:normal;opacity:.6}
.sec{padding:clamp(64px,9vw,120px) 0}
.sec.alt{background:var(--surface)}
.sec-head{max-width:760px;margin-bottom:40px}
.grid{display:grid;gap:22px;list-style:none;padding:0;margin:0}
.cards{grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}
.card{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;transition:transform .3s,box-shadow .3s}
.card:hover{transform:translateY(-6px);box-shadow:0 24px 50px -28px color-mix(in srgb,var(--primary) 70%,#000)}
.ph{position:relative;aspect-ratio:4/3;background:linear-gradient(135deg,var(--accent-soft),var(--accent));overflow:hidden}
.ph img{width:100%;height:100%;object-fit:cover;transition:transform .5s}
.card:hover .ph img{transform:scale(1.05)}
.ph.noimg::after{content:attr(data-initials);position:absolute;inset:0;display:grid;place-items:center;font:var(--hw) var(--hs) 64px/1 var(--head);color:var(--ink);opacity:.5}
.tag{position:absolute;left:12px;top:12px;z-index:1;padding:6px 11px;border-radius:999px;background:var(--primary);color:var(--on-primary);font:700 12px/1 var(--body);letter-spacing:.06em;text-transform:uppercase}
.card-body{display:flex;flex-direction:column;flex:1;padding:20px 20px 22px}
.card-body .muted{font-size:15.5px}
.card-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:auto;padding-top:8px}
.price{margin:0;font:800 21px/1 var(--body)}
.band{background:var(--band);color:var(--on-band);padding:clamp(56px,8vw,100px) 0;overflow:hidden}
.band-in{max-width:980px}
.band-title{font-size:clamp(38px,6.4vw,92px);margin:0 0 18px}
.band-text{font-size:clamp(18px,1.7vw,22px);opacity:.88;max-width:40em;margin:0}
.benefits{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.benefit{padding:26px;border-radius:var(--r);border:1px solid var(--line);background:var(--bg)}
.ico{width:46px;height:46px;padding:10px;border-radius:14px;background:var(--accent-soft);color:var(--ink);margin-bottom:18px}
.steps{grid-template-columns:repeat(auto-fit,minmax(240px,1fr));counter-reset:s}
.step{position:relative;padding:28px 26px;border-radius:var(--r);background:var(--bg);border:1px solid var(--line)}
.num{display:block;font:var(--hw) var(--hs) 54px/1 var(--head);color:var(--ink);margin-bottom:14px}
.faq details{border-bottom:1px solid var(--line);padding:4px 0}
.faq summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;gap:16px;padding:18px 0;font:700 19px/1.35 var(--body)}
.faq summary::-webkit-details-marker{display:none}
.faq summary::after{content:"+";font-size:26px;line-height:1;color:var(--ink);transition:rotate .3s}
.faq details[open] summary::after{rotate:45deg}
.faq details p{color:var(--muted);padding-bottom:16px;margin:0}
.cta{background:linear-gradient(160deg,var(--surface),var(--bg))}
.cta-in{display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:start}
.contacts{list-style:none;padding:0;margin:26px 0 0;display:grid;gap:14px}
.contacts span{display:block;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.contacts a,.contacts p{font-weight:700;font-size:19px;margin:0;text-decoration:none}
.form{display:grid;gap:8px;padding:28px;border-radius:var(--r);background:var(--bg);border:1px solid var(--line);box-shadow:0 30px 60px -40px color-mix(in srgb,var(--primary) 60%,#000)}
.form label{font-weight:600;font-size:15px;margin-top:6px}
.form input,.form textarea{width:100%;padding:13px 14px;border-radius:calc(var(--r) * .5);border:1.5px solid var(--line);background:var(--surface);color:var(--text);font:16px var(--body)}
.form input:focus,.form textarea:focus{outline:none;border-color:var(--ink)}
.form .btn{margin-top:12px}
.form-note{min-height:1.2em;margin:4px 0 0;color:var(--ink);font-weight:600}
.foot{border-top:1px solid var(--line);padding:28px 0;color:var(--muted);font-size:15px}
.foot-in{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
.foot a{color:var(--muted)}
.scrim{position:fixed;inset:0;z-index:40;background:rgba(0,0,0,.45)}
.cart{position:fixed;z-index:50;top:0;right:0;bottom:0;width:min(420px,100%);display:flex;flex-direction:column;background:var(--bg);border-left:1px solid var(--line);padding:22px;box-shadow:-30px 0 60px -30px rgba(0,0,0,.5);animation:slide .35s cubic-bezier(.2,.8,.2,1)}
.cart-head{display:flex;justify-content:space-between;align-items:center}
.cart-head h2{font-size:30px;margin:0}
.x{width:44px;height:44px;border-radius:12px;border:1.5px solid var(--line);background:none;color:var(--text);font-size:26px;cursor:pointer}
.cart-list{list-style:none;padding:0;margin:18px 0;display:grid;gap:12px;overflow:auto}
.cart-list li{display:grid;grid-template-columns:1fr auto;gap:4px 12px;padding:12px 0;border-bottom:1px solid var(--line)}
.cart-list .q{display:flex;align-items:center;gap:8px}
.cart-list .q button{width:34px;height:34px;border-radius:10px;border:1.5px solid var(--line);background:none;color:var(--text);cursor:pointer;font-size:18px}
.cart-empty{color:var(--muted)}
.cart-foot{margin-top:auto;display:grid;gap:12px;font-size:19px}
.js [data-reveal]{opacity:0;transform:translateY(26px);transition:opacity .7s ease,transform .7s cubic-bezier(.2,.8,.2,1);transition-delay:calc(var(--d,0) * 90ms)}
.js [data-reveal].in{opacity:1;transform:none}
@keyframes rise{from{opacity:0;transform:translateY(.45em) skewY(4deg)}to{opacity:1;transform:none}}
@keyframes spin{to{rotate:360deg}}
@keyframes float{50%{translate:0 -12px}}
@keyframes marquee{to{transform:translateX(-50%)}}
@keyframes slide{from{transform:translateX(100%)}}
@keyframes bump{40%{transform:scale(1.35)}}
@media (max-width:900px){
 .nav{position:absolute;left:0;right:0;top:100%;flex-direction:column;gap:0;padding:8px 20px 18px;background:var(--bg);border-bottom:1px solid var(--line);display:none}
 .nav.open{display:flex}
 .nav a{padding:13px 0;border-bottom:1px solid var(--line)}
 .menu-btn{display:flex}
 .top-acts{margin-left:auto}
 .hero-in,.cta-in{grid-template-columns:1fr}
 .visual{width:min(78%,360px);order:-1}
}
@media (max-width:520px){
 body{font-size:16px}
 .wrap,.narrow{width:calc(100% - 32px)}
 .top-in{gap:12px}
 .logo{font-size:19px}
 .cart-label{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
 .cart-btn{padding:8px 12px}
 .top-acts .btn.small{display:none}
 .acts .btn{flex:1 1 100%}
 .form{padding:20px}
}
@media (prefers-reduced-motion:reduce){
 *,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}
 .js [data-reveal]{opacity:1;transform:none}
}
"""

JS = r"""
(function(){
  "use strict";
  var $=function(id){return document.getElementById(id)};
  var reveal=document.querySelectorAll("[data-reveal]");
  if("IntersectionObserver" in window){
    var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add("in");io.unobserve(e.target)}})},{threshold:.12,rootMargin:"0px 0px -40px 0px"});
    reveal.forEach(function(el){io.observe(el)});
  }else reveal.forEach(function(el){el.classList.add("in")});
  var menu=$("menuBtn"),nav=$("nav");
  if(menu){menu.onclick=function(){var o=nav.classList.toggle("open");menu.setAttribute("aria-expanded",String(o))};
    nav.addEventListener("click",function(e){if(e.target.tagName==="A"){nav.classList.remove("open");menu.setAttribute("aria-expanded","false")}})}
  var form=$("leadForm"),note=$("formNote"),msg=$("fMsg");
  document.querySelectorAll("[data-pick]").forEach(function(a){a.addEventListener("click",function(){msg.value="Хочу заказать: "+a.getAttribute("data-pick");})});
  form.addEventListener("submit",function(e){
    e.preventDefault();
    var email=form.getAttribute("data-email"),brand=form.getAttribute("data-brand");
    var body="Имя: "+$("fName").value+"\nКонтакт: "+$("fContact").value+"\n\n"+msg.value;
    if(email){location.href="mailto:"+email+"?subject="+encodeURIComponent("Заявка с сайта "+brand)+"&body="+encodeURIComponent(body);note.textContent="Открываю почту — проверьте письмо и отправьте."}
    else note.textContent="Спасибо! Заявка принята — мы свяжемся с вами.";
  });
  var dataEl=$("products-data");
  if(!dataEl)return;
  var items=JSON.parse(dataEl.textContent||"[]"),cart={},KEY="cart:"+location.pathname;
  try{cart=JSON.parse(localStorage.getItem(KEY)||"{}")||{}}catch(e){cart={}}
  var fmt=function(v,c){return new Intl.NumberFormat("ru-RU",{maximumFractionDigits:2}).format(v)+(c?" "+c:"")};
  function save(){try{localStorage.setItem(KEY,JSON.stringify(cart))}catch(e){}}
  function draw(){
    var list=$("cartList"),n=0,total=0,cur="";list.innerHTML="";
    Object.keys(cart).forEach(function(k){
      var it=items[k],q=cart[k];if(!it||q<1)return;n+=q;total+=it.price*q;cur=cur||it.cur;
      var li=document.createElement("li"),name=document.createElement("b");name.textContent=it.name;
      var sum=document.createElement("span");sum.textContent=fmt(it.price*q,it.cur);
      var box=document.createElement("div");box.className="q";
      [["−",-1,"Убрать одну"],["+",1,"Добавить ещё"]].forEach(function(b,i){var btn=document.createElement("button");btn.type="button";btn.textContent=b[0];btn.setAttribute("aria-label",b[2]+": "+it.name);btn.onclick=function(){cart[k]=(cart[k]||0)+b[1];if(cart[k]<1)delete cart[k];save();draw()};box.appendChild(btn);if(i===0){var s=document.createElement("span");s.textContent=q;box.appendChild(s)}});
      li.append(name,sum,box);list.appendChild(li);
    });
    $("cartCount").textContent=n;$("cartTotal").textContent=fmt(total,cur);$("cartEmpty").hidden=n>0;$("checkout").disabled=n===0;
  }
  function openCart(o){$("cart").hidden=!o;$("scrim").hidden=!o;$("cartBtn").setAttribute("aria-expanded",String(o));if(o)$("cartClose").focus()}
  document.querySelectorAll("[data-add]").forEach(function(b){b.addEventListener("click",function(){
    var k=b.getAttribute("data-add");cart[k]=(cart[k]||0)+1;save();draw();
    var c=$("cartCount");c.classList.remove("bump");void c.offsetWidth;c.classList.add("bump");
    b.textContent="Добавлено ✓";setTimeout(function(){b.textContent="В корзину"},1400);
  })});
  $("cartBtn").onclick=function(){openCart(true)};$("cartClose").onclick=$("scrim").onclick=function(){openCart(false)};
  document.addEventListener("keydown",function(e){if(e.key==="Escape")openCart(false)});
  $("checkout").onclick=function(){
    var lines=Object.keys(cart).map(function(k){return items[k].name+" × "+cart[k]});
    msg.value="Заказ:\n"+lines.join("\n")+"\nИтого: "+$("cartTotal").textContent;
    openCart(false);$("contact").scrollIntoView();$("fName").focus();
  };
  draw();
})();
"""
