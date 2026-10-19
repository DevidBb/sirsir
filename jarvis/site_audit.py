"""Разбор существующего сайта и проверка качества («до → после») в браузере Джарвиса.

Всё считается по-настоящему на открытой странице: заголовки и описание (SEO), вёрстка на
ширине телефона, подписи картинок и кнопок (доступность), вес и число запросов. Оценки —
доля пройденных проверок, без выдуманных «конверсий». Текст чужой страницы — недоверенные данные:
модель получает его только как сведения о бизнесе.
"""

from __future__ import annotations

import colorsys
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DESKTOP = {"width": 1366, "height": 860}
MOBILE = {"width": 390, "height": 844}
CATEGORIES = ("seo", "mobile", "a11y", "speed")
LABELS = {"seo": "SEO", "mobile": "Мобильная версия", "a11y": "Доступность", "speed": "Вес и запросы"}

# Факты для оценки качества — одинаково для старого и нового сайта.
AUDIT_JS = r"""
() => {
  const q = (s) => [...document.querySelectorAll(s)];
  const imgs = q('img');
  const nav = performance.getEntriesByType('navigation')[0];
  const res = performance.getEntriesByType('resource');
  const size = (e) => e.transferSize || e.encodedBodySize || 0;
  const bytes = (nav ? size(nav) : 0) + res.reduce((s, r) => s + size(r), 0);
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const named = (el) => !!((el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText ||
    el.getAttribute('aria-labelledby') || (el.querySelector('img[alt]:not([alt=""]), svg[aria-label]') ? 'img' : '') || '').trim());
  const controls = q('a[href], button').filter(shown);
  const inputs = q('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]), textarea, select').filter(shown);
  const labelled = inputs.filter(el => (el.labels && el.labels.length) || el.getAttribute('aria-label') ||
    el.getAttribute('aria-labelledby') || el.getAttribute('title'));
  const meta = (sel) => { const m = document.querySelector(sel); return m ? (m.getAttribute('content') || '').trim() : ''; };
  const small = window.innerWidth < 600 ? controls.filter(c => {
    const r = c.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.height < 32 && r.width < 32;
  }).length : 0;
  return {
    title: (document.title || '').trim(), description: meta('meta[name="description"]'),
    lang: document.documentElement.getAttribute('lang') || '', viewport: !!document.querySelector('meta[name="viewport"]'),
    h1: q('h1').length, og: !!document.querySelector('meta[property="og:title"]'),
    images: imgs.length, imagesNoAlt: imgs.filter(i => !i.hasAttribute('alt')).length,
    controls: controls.length, controlsUnnamed: controls.filter(c => !named(c)).length,
    inputs: inputs.length, inputsUnlabelled: inputs.length - labelled.length,
    bytes, requests: res.length + 1, loadMs: nav ? Math.round(nav.loadEventEnd || nav.domContentLoadedEventEnd || 0) : 0,
    overflow: document.documentElement.scrollWidth > window.innerWidth + 2,
    bodyFont: parseFloat(getComputedStyle(document.body || document.documentElement).fontSize) || 16,
    smallTaps: small, width: window.innerWidth,
  };
}
"""

# Сведения о бизнесе со страницы: тексты, разделы, цвета, картинки, товары с ценами, контакты.
ANALYZE_JS = r"""
() => {
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const meta = (n) => { const m = document.querySelector(`meta[name="${n}"], meta[property="${n}"]`); return m ? (m.getAttribute('content') || '').trim() : ''; };
  const heads = [...document.querySelectorAll('h1, h2, h3')].map(txt).filter(t => t && t.length < 140).slice(0, 24);
  const nav = [...new Set([...document.querySelectorAll('nav a, header a')].map(txt).filter(t => t && t.length < 32))].slice(0, 18);
  const weight = {};
  const add = (c, w) => { if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') weight[c] = (weight[c] || 0) + w; };
  [...document.querySelectorAll('button, a, header, nav, [class*=btn], [class*=button], [class*=badge], [class*=tag], h1, h2')]
    .slice(0, 500).forEach(el => { const s = getComputedStyle(el); add(s.backgroundColor, 3); add(s.color, 1); add(s.borderTopColor, 0.5); });
  const colors = Object.entries(weight).sort((a, b) => b[1] - a[1]).slice(0, 16).map(([c, w]) => ({ c, w }));
  const fonts = [...new Set(['h1', 'h2', 'body', 'button'].map(s => document.querySelector(s)).filter(Boolean)
    .map(el => getComputedStyle(el).fontFamily.split(',')[0].replace(/["']/g, '').trim()))].slice(0, 4);
  const src = (im) => im.currentSrc || im.src || '';
  const images = [...document.images].filter(im => im.naturalWidth >= 240 && im.naturalHeight >= 180 && /^https?:/.test(src(im)))
    .slice(0, 30).map(im => ({ src: src(im), alt: (im.alt || '').slice(0, 80), w: im.naturalWidth, h: im.naturalHeight }));
  const logoEl = document.querySelector('header img, [class*=logo] img, img[alt*=logo i], img[class*=logo i], img[src*=logo i]');
  const priceRe = /(\d[\d\s .,]*\s?(?:₽|руб\.?|р\.|€|\$|eur|usd|₸))|((?:€|\$)\s?\d[\d.,]*)/i;
  const products = []; const seen = new Set();
  for (const el of document.querySelectorAll('[class*=product], [class*=card], [class*=item], [class*=tile], li, article')) {
    if (products.length >= 12) break;
    const t = txt(el); if (!t || t.length > 320) continue;
    const m = t.match(priceRe); if (!m) continue;
    const nameEl = el.querySelector('h2, h3, h4, [class*=title], [class*=name]');
    let name = txt(nameEl) || t.split(m[0])[0].trim();
    name = name.slice(0, 80);
    if (!name || name.length < 3 || seen.has(name)) continue;
    seen.add(name);
    const img = el.querySelector('img');
    products.push({ name, price: m[0].trim(), image: img && /^https?:/.test(src(img)) ? src(img) : '' });
  }
  const tel = [...new Set([...document.querySelectorAll('a[href^="tel:"]')].map(a => a.getAttribute('href').slice(4).trim()))].slice(0, 2);
  const mail = [...new Set([...document.querySelectorAll('a[href^="mailto:"]')].map(a => a.getAttribute('href').slice(7).split('?')[0].trim()))].slice(0, 2);
  const main = document.querySelector('main, [role=main]') || document.body;
  return {
    url: location.href, title: document.title || '', description: meta('description') || meta('og:description'),
    siteName: meta('og:site_name'), ogImage: meta('og:image'), lang: document.documentElement.lang || '',
    heads, nav, colors, fonts, images, logo: logoEl && /^https?:/.test(src(logoEl)) ? src(logoEl) : '',
    products, tel, mail, text: txt(main).slice(0, 2500),
  };
}
"""


# --- оценки -----------------------------------------------------------------
def _check(name: str, ok: bool) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok)}


def score(desktop: dict[str, Any], mobile: dict[str, Any], extra_bytes: int = 0) -> dict[str, dict[str, Any]]:
    """Оценки 0–100 по категориям: доля пройденных проверок (вес — среднее двух порогов)."""
    d, m = desktop or {}, mobile or {}
    images = int(d.get("images") or 0)
    alt_ok = images == 0 or int(d.get("imagesNoAlt") or 0) <= images * 0.1
    controls = int(d.get("controls") or 0)
    title_len = len(str(d.get("title") or ""))
    desc_len = len(str(d.get("description") or ""))
    seo = [
        _check("Заголовок страницы 10–70 символов", 10 <= title_len <= 70),
        _check("Описание для поисковиков 50–170 символов", 50 <= desc_len <= 170),
        _check("Ровно один главный заголовок (h1)", d.get("h1") == 1),
        _check("Указан язык страницы", bool(d.get("lang"))),
        _check("Карточка для соцсетей (og:title)", bool(d.get("og"))),
        _check("Подписи у картинок", alt_ok),
    ]
    mob = [
        _check("Настроен масштаб для телефона", bool(d.get("viewport"))),
        _check("Нет прокрутки вбок на телефоне", not m.get("overflow", True)),
        _check("Шрифт на телефоне не меньше 14 px", float(m.get("bodyFont") or 0) >= 14),
        _check("Кнопки и ссылки удобно нажимать пальцем", int(m.get("smallTaps") or 0) <= 3),
    ]
    a11y = [
        _check("Подписи у картинок", alt_ok),
        _check("У кнопок и ссылок есть названия",
               controls == 0 or int(d.get("controlsUnnamed") or 0) <= max(1, controls * 0.02)),
        _check("У полей ввода есть подписи", int(d.get("inputsUnlabelled") or 0) == 0),
        _check("Указан язык страницы", bool(d.get("lang"))),
    ]
    mb = (int(d.get("bytes") or 0) + extra_bytes) / 1_000_000
    requests = int(d.get("requests") or 0)
    weight_score = 100 if mb <= 1.5 else 75 if mb <= 3 else 50 if mb <= 6 else 25
    request_score = 100 if requests <= 40 else 75 if requests <= 80 else 50 if requests <= 150 else 25
    speed = [
        _check(f"Вес страницы ≈ {mb:.1f} МБ (хорошо — до 1,5 МБ)".replace(".", ","), mb <= 1.5),
        _check(f"Запросов к серверу: {requests} (хорошо — до 40)", requests <= 40),
    ]

    def pct(checks: list[dict[str, Any]]) -> int:
        return round(100 * sum(c["ok"] for c in checks) / len(checks))
    return {
        "seo": {"score": pct(seo), "checks": seo},
        "mobile": {"score": pct(mob), "checks": mob},
        "a11y": {"score": pct(a11y), "checks": a11y},
        "speed": {"score": round((weight_score + request_score) / 2), "checks": speed,
                  "mb": round(mb, 2), "requests": requests},
    }


def compare(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    """Строки «до → после» для HUD и ответа."""
    rows = []
    for key in CATEGORIES:
        rows.append({"key": key, "label": LABELS[key],
                     "before": (before or {}).get(key, {}).get("score") if before else None,
                     "after": after.get(key, {}).get("score")})
    return rows


# --- цвета бренда -------------------------------------------------------------------
def css_color_to_hex(value: str) -> str | None:
    m = re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+%?))?\s*\)", value or "")
    if not m:
        return None
    alpha = m.group(4)
    if alpha is not None:
        a = float(alpha.rstrip("%")) / (100 if alpha.endswith("%") else 1)
        if a < 0.6:
            return None
    r, g, b = (int(m.group(i)) for i in (1, 2, 3))
    return f"#{r:02x}{g:02x}{b:02x}"


def _hsv(color: str) -> tuple[float, float, float]:
    h = color.lstrip("#")
    return colorsys.rgb_to_hsv(int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def brand_colors(analysis: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Самый частый насыщенный цвет на кнопках и в шапке — фирменный; следующий заметно другой
    (по оттенку или насыщенности, например розовый к красному) — акцент."""
    found: list[str] = []
    for item in (analysis or {}).get("colors") or []:
        color = css_color_to_hex(str(item.get("c", "")))
        if color is None or color in found:
            continue
        _, s, v = _hsv(color)
        if s >= 0.22 and v >= 0.25:
            found.append(color)
    strong = [c for c in found if _hsv(c)[1] >= 0.35]
    if not strong:
        return None, None
    primary = strong[0]
    hue, sat, _ = _hsv(primary)
    for color in found:
        h, s, _ = _hsv(color)
        if color != primary and (min(abs(h - hue), 1 - abs(h - hue)) > 0.07 or abs(s - sat) > 0.3):
            return primary, color
    return primary, None


def business_notes(analysis: dict[str, Any] | None, limit: int = 3500) -> str:
    """Короткая выжимка сайта для модели (недоверенные данные)."""
    a = analysis or {}
    lines = [f"Адрес: {a.get('url', '')}", f"Заголовок: {a.get('title', '')}",
             f"Описание: {a.get('description', '')}"]
    if a.get("heads"):
        lines.append("Заголовки разделов: " + " | ".join(a["heads"][:16]))
    if a.get("nav"):
        lines.append("Меню: " + ", ".join(a["nav"][:14]))
    if a.get("products"):
        lines.append("Товары и цены: " + "; ".join(f"{p['name']} — {p['price']}" for p in a["products"][:10]))
    contacts = [*(a.get("tel") or []), *(a.get("mail") or [])]
    if contacts:
        lines.append("Контакты: " + ", ".join(contacts))
    if a.get("text"):
        lines.append("Текст страницы: " + a["text"][:1800])
    return "\n".join(line for line in lines if line.split(": ", 1)[-1])[:limit]


# --- проверка в браузере --------------------------------------------------------------
def inspect(browser: Any, url: str, shots_dir: Path, prefix: str, analyze: bool = False,
            local: bool = False) -> dict[str, Any]:
    """Открыть адрес во временной вкладке: факты на компьютере и телефоне, снимки, (разбор сайта).

    local=True — файл сайта, который сделал сам Джарвис (file://); иначе только http(s).
    """
    from .browser import check_url
    if not local:
        url = check_url(url)
    elif not url.startswith("file:"):
        raise ValueError("local=True только для file://")
    shots_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    desktop_shot = shots_dir / f"{prefix}-{stamp}-desktop.png"
    mobile_shot = shots_dir / f"{prefix}-{stamp}-mobile.png"

    def job(page) -> dict[str, Any]:
        page.set_viewport_size(DESKTOP)
        response = page.goto(url, wait_until="domcontentloaded")
        for state, timeout in (("load", 10000), ("networkidle", 4000)):
            try:
                page.wait_for_load_state(state, timeout=timeout)
            except Exception:  # noqa: BLE001 — реклама и видео не дают сети затихнуть
                pass
        page.wait_for_timeout(1300)  # анимации появления
        result: dict[str, Any] = {"url": page.url, "status": response.status if response else None}
        result["desktop"] = page.evaluate(AUDIT_JS)
        if analyze:
            result["analysis"] = page.evaluate(ANALYZE_JS)
        page.screenshot(path=str(desktop_shot))
        page.set_viewport_size(MOBILE)
        page.wait_for_timeout(900)
        result["mobile"] = page.evaluate(AUDIT_JS)
        page.screenshot(path=str(mobile_shot))
        result["shots"] = {"desktop": desktop_shot.name, "mobile": mobile_shot.name}
        return result

    return browser.with_temp_page(job, timeout=120)
