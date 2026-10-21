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

# Пролистать страницу вниз и вернуться наверх — как посетитель (замеры веса уже сделаны до этого).
SCROLL_JS = r"""
async () => {
  const end = Math.min(document.documentElement.scrollHeight, 15000);
  for (let y = 0; y < end; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
  window.scrollTo(0, 0);
}
"""

# Сведения о бизнесе со страницы: тексты, разделы, цвета, картинки, товары с ценами, контакты.
# Товар — видимая карточка со ссылкой на товар (/products/…, /product/…) вне шапки, меню и всплывающих
# окон, плюс товары из разметки JSON-LD; без таких ссылок — небольшие блоки «название + цена».
# Цена — первая ненулевая, не перечёркнутая и не «за кг». Логотип — видимая небольшая картинка
# с подсказкой «logo» или ссылкой на главную, иначе logo из JSON-LD.
ANALYZE_JS = r"""
() => {
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const meta = (n) => { const m = document.querySelector(`meta[name="${n}"], meta[property="${n}"]`); return m ? (m.getAttribute('content') || '').trim() : ''; };
  const abs = (u) => { try { return u ? new URL(u, location.href).href : ''; } catch (e) { return ''; } };
  const web = (u) => (/^https?:/.test(u || '') ? u : '');
  const shown = (el) => {
    if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') return false;
    for (let e = el; e && e !== document.body; e = e.parentElement) if (getComputedStyle(e).opacity === '0') return false;
    return true;
  };
  const within = (el, sel) => { const c = el.closest(sel); return !!c && c !== document.body && c !== document.documentElement; };
  // Меню, шторки, всплывающие окна и подвал; CHROME — ещё шапка и бегущая строка над ней.
  const OVERLAY = 'nav, footer, dialog, [role=navigation], [role=contentinfo], [role=dialog], [aria-modal=true], ' +
    '[class*=megamenu i], [class*=mega-menu i], [class*=submenu i], [class*=dropdown i], [class*=drawer i], ' +
    '[class*=modal i], [class*=popup i], [class*=predictive i], [class*=cookie i]';
  const CHROME = OVERLAY + ', header, [role=banner], [id*=header i], [class*=announcement i], [class*=utility-bar i]';
  const imgUrl = (im) => {
    const set = (im.getAttribute('srcset') || im.getAttribute('data-srcset') || '').trim().split(/\s+/)[0];
    for (const u of [im.currentSrc, im.src, im.getAttribute('data-src'), im.getAttribute('data-lazy-src'), im.getAttribute('data-original'), set]) {
      const w = web(abs(u)); if (w) return w;
    }
    return '';
  };
  const heads = [...document.querySelectorAll('h1, h2, h3')].filter(el => shown(el) && !within(el, OVERLAY))
    .map(txt).filter(t => t && t.length < 140).slice(0, 24);
  const navLinks = [...document.querySelectorAll('nav a, header a')];
  const navShown = navLinks.filter(shown);
  const nav = [...new Set((navShown.length >= 3 ? navShown : navLinks).map(txt).filter(t => t && t.length < 32))].slice(0, 18);
  const weight = {};
  const add = (c, w) => { if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') weight[c] = (weight[c] || 0) + w; };
  [...document.querySelectorAll('button, a, header, nav, [class*=btn], [class*=button], [class*=badge], [class*=tag], h1, h2')]
    .slice(0, 500).forEach(el => { const s = getComputedStyle(el); add(s.backgroundColor, 3); add(s.color, 1); add(s.borderTopColor, 0.5); });
  const colors = Object.entries(weight).sort((a, b) => b[1] - a[1]).slice(0, 16).map(([c, w]) => ({ c, w }));
  const fonts = [...new Set(['h1', 'h2', 'body', 'button'].map(s => document.querySelector(s)).filter(Boolean)
    .map(el => getComputedStyle(el).fontFamily.split(',')[0].replace(/["']/g, '').trim()))].slice(0, 4);
  const images = [...document.images]
    .filter(im => im.naturalWidth >= 240 && im.naturalHeight >= 180 && imgUrl(im) && shown(im) && !within(im, OVERLAY))
    .slice(0, 30).map(im => {
      const r = im.getBoundingClientRect();
      return { src: imgUrl(im), alt: (im.alt || '').slice(0, 80), w: im.naturalWidth, h: im.naturalHeight,
               rw: Math.round(r.width), rh: Math.round(r.height), top: Math.round(r.top + window.scrollY) };
    });

  // JSON-LD: товары (Product, в том числе внутри ItemList и @graph) и логотип организации.
  const ld = [];
  const walk = (v, depth) => {
    if (!v || typeof v !== 'object' || depth > 6) return;
    if (Array.isArray(v)) { v.forEach(x => walk(x, depth + 1)); return; }
    ld.push(v);
    for (const k of ['@graph', 'itemListElement', 'item', 'mainEntity', 'hasOfferCatalog', 'itemOffered']) walk(v[k], depth + 1);
  };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { walk(JSON.parse(s.textContent), 0); } catch (e) { /* битая разметка — пропускаем */ }
  }
  const types = (n) => [].concat(n['@type'] || []).map(String);
  const first = (v) => (Array.isArray(v) ? v[0] : v);
  const ldUrl = (v) => { v = first(v); return typeof v === 'string' ? v : (v && (v.url || v.contentUrl)) || ''; };
  const SYMBOLS = { EUR: '€', RUB: '₽', USD: '$', KZT: '₸', GBP: '£', UAH: '₴' };

  // Цены: «2,99 €», «€2.99», «1 290 ₽», «от 1500 руб.»; нулевые, перечёркнутые и «за кг» не берём.
  const NUM = /\d+(?:[\s.,]\d{3})*(?:[.,]\d{1,2})?/.source;
  // \b в JS не работает с кириллицей: после «руб», «тг», «грн» не должно быть буквы («2 Рублёвское ш.»).
  const CUR = /₽|руб(?:лей|ля|ль)?(?:\.|(?![а-яё]))|р\.|€|\$|£|₸|тг(?:\.|(?![а-яё]))|грн(?:\.|(?![а-яё]))|₴|eur\b|usd\b|rub\b|kzt\b/.source;
  const PRICE = new RegExp('(?:(?:€|\\$|£)\\s?(?:' + NUM + ')|(?:' + NUM + ')\\s?(?:' + CUR + '))', 'i');
  const STRUCK = 's, del, strike, [class*=strike i], [class*=compare i], [class*=crossed i], [class*=old-price i], ' +
    '[class*=price-old i], [class*=price--old i], [class*=oldprice i], [class*=was-price i], [class*=unit-price i], ' +
    '[class*=unit_price i], [class*=unitprice i], [class*=price-unit i], [class*=base-price i], [class*=baseprice i]';
  const struck = (el, card) => {
    for (let e = el; e && e !== card.parentElement; e = e.parentElement) {
      if (e.matches(STRUCK) || getComputedStyle(e).textDecorationLine.includes('line-through')) return true;
    }
    return false;
  };
  const visibleText = (card) => {
    const parts = []; const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT); let n;
    while ((n = walker.nextNode())) {
      const el = n.parentElement;
      if (!el || !n.nodeValue.trim() || el.closest('script, style, template, noscript')) continue;
      if (shown(el) && !struck(el, card)) parts.push(n.nodeValue.trim());
    }
    return parts.join(' ').replace(/\s+/g, ' ');
  };
  const pricesIn = (text) => {
    const out = []; const re = new RegExp(PRICE.source, 'gi'); let m;
    while ((m = re.exec(text))) {
      const after = text.slice(re.lastIndex, re.lastIndex + 16);
      const before = text.slice(Math.max(0, m.index - 24), m.index);
      if (!/[1-9]/.test(m[0]) || /^\s*(?:\/|pro\s|per\s|je\s|за\s*(?:\d|кг|г\b|л\b|шт))/i.test(after) ||
          /(?:grundpreis|unit price|цена за)\s*:?\s*$/i.test(before)) continue;
      const from = /(?:^|\s)(от|from|ab)\s*$/i.exec(before);
      out.push((from ? from[1] + ' ' : '') + m[0].replace(/\s+/g, ' ').trim());
    }
    return out;
  };
  const okName = (t) => t.length >= 3 && t.length <= 160 && /\p{L}{2}/u.test(t) && !PRICE.test(t);
  const PRODUCT_PATH = /\/(?:products?|tproduct|goods)\/[^/?#]+/i;
  const keyOf = (a) => {
    const m = /^https?:/.test(a.href || '') && (a.pathname || '').match(PRODUCT_PATH);
    return m ? a.host + m[0].toLowerCase() : '';
  };
  const nameOf = (card) => {
    const groups = [card.querySelectorAll('[itemprop=name]'), card.querySelectorAll('h1, h2, h3, h4, h5, h6'),
      [...card.querySelectorAll('a[href]')].filter(keyOf),
      [...card.querySelectorAll('[class*=title i], [class*=name i], [class*=heading i]')].filter(el => !/subtitle/i.test(el.className))];
    for (const group of groups) {
      for (const el of group) { const t = txt(el); if (shown(el) && okName(t)) return t; }
    }
    for (const im of card.querySelectorAll('img[alt]')) {
      const t = im.alt.trim(); if (okName(t) && !/_|\.\w{3,4}$/.test(t)) return t;  // не «More_Pudding_4096x4096.png»
    }
    return '';
  };
  const imageOf = (card) => {
    const imgs = [...card.querySelectorAll('img')].filter(im => imgUrl(im) && shown(im) && im.getBoundingClientRect().width >= 60);
    imgs.sort((a, b) => { const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect(); return rb.width * rb.height - ra.width * ra.height; });
    return imgs.length ? imgUrl(imgs[0]) : '';
  };

  const products = []; const seen = new Set();
  const addProduct = (p) => {
    const name = p.name.length > 80 ? p.name.slice(0, 80).replace(/[\s,.:;—-]+\S*$/, '') + '…' : p.name;
    const key = name.toLowerCase();
    if (products.length >= 12 || !okName(name) || seen.has(key)) return;
    seen.add(key); products.push({ name, price: p.price, image: p.image });
  };
  // 1) Карточки со ссылкой на товар: самый большой блок вокруг ссылки, где нет других товаров.
  //    Есть карточки с ценами — карточки без цены (отзывы, подборки со ссылкой на товар) не берём.
  const done = new Set(); const linked = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (linked.length >= 24) break;
    const key = keyOf(a);
    if (!key || done.has(key) || within(a, CHROME) || !shown(a)) continue;
    let card = a;
    while (card.parentElement && card.parentElement !== document.body && card.parentElement.tagName !== 'MAIN') {
      const up = card.parentElement;
      if (txt(up).length > 600 || new Set([...up.querySelectorAll('a[href]')].map(keyOf).filter(Boolean)).size > 1) break;
      card = up;
    }
    const name = nameOf(card); const price = pricesIn(visibleText(card))[0] || ''; const image = imageOf(card);
    if (!name || (!price && !image)) continue;
    done.add(key);
    linked.push({ name, price, image });
  }
  const priced = linked.filter(p => p.price);
  (priced.length ? priced : linked).forEach(addProduct);
  // 2) Товары из JSON-LD.
  for (const n of ld.filter(n => types(n).includes('Product'))) {
    const offer = first(n.offers) || {};
    const value = offer.price ?? offer.lowPrice ?? (first(offer.priceSpecification) || {}).price;
    const cur = String(offer.priceCurrency || '').toUpperCase();
    addProduct({ name: String(n.name || '').replace(/\s+/g, ' ').trim(),
                 price: value != null && /[1-9]/.test(String(value)) ? `${value} ${SYMBOLS[cur] || cur}`.trim() : '',
                 image: web(abs(ldUrl(n.image))) });
  }
  // 3) Нет ссылок на товары (услуги, меню, лендинги): самые маленькие блоки «название + цена».
  if (products.length < 3) {
    const found = [];
    for (const el of document.querySelectorAll('[class*=product i], [class*=card i], [class*=item i], [class*=tile i], ' +
                                               '[class*=service i], [class*=price i], [itemtype*=Product], li, article')) {
      if (found.length >= 60) break;
      const t = txt(el);
      if (!t || t.length > 320 || within(el, CHROME) || !shown(el)) continue;
      const prices = pricesIn(visibleText(el));
      if (!prices.length || prices.length > 2) continue;
      let name = nameOf(el);
      if (!name) {
        const lead = visibleText(el).split(prices[0].replace(/^(?:от|from|ab) /i, ''))[0]
          .replace(/(?:^|\s)(?:от|from|ab|bis|до)\s*$/i, '').trim();
        name = lead.length <= 60 ? lead : '';
      }
      if (name) found.push({ el, name, price: prices[0], image: imageOf(el) });
    }
    const blocks = found.filter(f => !found.some(o => o !== f && f.el.contains(o.el)));
    if (blocks.length >= 2) blocks.forEach(addProduct);  // одна пара «имя + цена» — скорее случайность, чем прайс
  }

  // Логотип: видимая небольшая картинка с подсказкой «logo» или ссылкой на главную, выше по странице — лучше.
  let logo = '', best = 0;
  const siteName = (meta('og:site_name') || '').toLowerCase();
  for (const im of [...document.images].slice(0, 300)) {
    const url = imgUrl(im);
    if (!url || within(im, 'footer, [role=contentinfo]') || !shown(im)) continue;
    const r = im.getBoundingClientRect(); const top = r.top + window.scrollY;
    if (r.width > 420 || r.height > 180 || r.width < 16 || r.height < 10 || top > 1500) continue;
    const link = im.closest('a');
    const hint = `${im.alt} ${im.className} ${url.split('?')[0]} ${im.parentElement.className} ${link ? link.className : ''}`.toLowerCase();
    let s = 0;
    if (/logo/.test(hint)) s += 3;
    if (link && link.host === location.host && /^\/(?:index\.\w+)?$/.test(link.pathname || '')) s += 3;
    if (s === 0) continue;
    if (within(im, 'header, [role=banner], [id*=header i], [class*=header i]')) s += 2;
    if (siteName && (im.alt || '').toLowerCase().includes(siteName)) s += 1;
    s -= top / 1500;
    if (s > best) { best = s; logo = url; }
  }
  if (!logo) {
    const org = ld.find(n => n.logo && types(n).some(t => /Organization|Corporation|Brand|Store|Business|Restaurant|Shop/i.test(t)));
    logo = org ? web(abs(ldUrl(org.logo))) : '';
    if (/share|social|banner|cover/i.test(logo.split('?')[0].split('/').pop())) logo = '';  // картинка для соцсетей
  }
  const tel = [...new Set([...document.querySelectorAll('a[href^="tel:"]')].map(a => a.getAttribute('href').slice(4).trim()))].slice(0, 2);
  const mail = [...new Set([...document.querySelectorAll('a[href^="mailto:"]')].map(a => a.getAttribute('href').slice(7).split('?')[0].trim()))].slice(0, 2);
  const main = document.querySelector('main, [role=main]') || document.body;
  return {
    url: location.href, title: document.title || '', description: meta('description') || meta('og:description'),
    siteName: meta('og:site_name'), ogImage: meta('og:image'), lang: document.documentElement.lang || '',
    heads, nav, colors, fonts, images, logo, products, tel, mail, text: txt(main).slice(0, 2500),
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
    lines = [f"Сайт: {a.get('url', '')}", f"Заголовок: {a.get('title', '')}",
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
            page.evaluate(SCROLL_JS)  # блоки «появляются при прокрутке», картинки грузятся лениво
            page.wait_for_timeout(800)
            result["analysis"] = page.evaluate(ANALYZE_JS)
        page.screenshot(path=str(desktop_shot))
        page.set_viewport_size(MOBILE)
        page.wait_for_timeout(900)
        result["mobile"] = page.evaluate(AUDIT_JS)
        page.screenshot(path=str(mobile_shot))
        result["shots"] = {"desktop": desktop_shot.name, "mobile": mobile_shot.name}
        return result

    return browser.with_temp_page(job, timeout=120)
