import json
import re

import pytest

from conftest import FakeLLM, text
from jarvis import site_audit
from jarvis.assistant import Assistant
from jarvis.memory import Memory
from jarvis.site_render import contrast, palette, parse_price, render
from jarvis.site_studio import (SiteStudio, base_spec, extract_json, guess_direction, match_site,
                                normalize_spec, parse_revision, parse_site_request, quick_edit,
                                spoken_domain)
from jarvis.tool_registry import ToolRegistry
from jarvis.web_search import SearchResult


# --- разбор фраз ---------------------------------------------------------------
def test_new_site_phrase_like_in_the_video():
    req = parse_site_request("Джарвис, сделай мне новый сайт для More Nutrition за три часа")
    assert req.target == "More Nutrition"
    assert req.deadline == "за три часа"
    assert req.url == "" and not req.redo


@pytest.mark.parametrize("phrase, target, details, url", [
    ("сделай сайт для кофейни «Зерно», мы варим кофе с собой у метро", "кофейни «Зерно»",
     "мы варим кофе с собой у метро", ""),
    ("переделай сайт morenutrition.de", "", "", "https://morenutrition.de"),
    ("создай лендинг для барбершопа Бритва в турбо режиме", "барбершопа Бритва", "", ""),
    ("нужен сайт для пекарни Колос", "пекарни Колос", "", ""),
    ("сделай новый крутой сайт для www.example.ru за 30 секунд", "", "", "https://www.example.ru"),
    ("сверстай одностраничный сайт для студии йоги Прана", "студии йоги Прана", "", ""),
])
def test_site_phrases(phrase, target, details, url):
    req = parse_site_request(phrase)
    assert req is not None
    assert (req.target, req.details, req.url) == (target, details, url)


def test_turbo_and_redo_flags():
    assert parse_site_request("создай лендинг для барбершопа Бритва в турбо режиме").turbo
    assert parse_site_request("переделай сайт morenutrition.de").redo


def test_spoken_domain():
    assert spoken_domain("переделай сайт example точка ру") == "переделай сайт example.ru"
    req = parse_site_request("переделай сайт coffee-zerno точка ком")
    assert req.url == "https://coffee-zerno.com"


@pytest.mark.parametrize("phrase", [
    "открой сайт ютуба", "сделай скриншот", "сделай громче", "найди сайт кофейни", "какой сайт лучше",
    "сделай сайтом по умолчанию хром",
])
def test_not_site_requests(phrase):
    assert parse_site_request(phrase) is None


@pytest.mark.parametrize("phrase, what", [
    ("измени сайт: сделай тёмную тему", "сделай тёмную тему"),
    ("поменяй сайт, добавь раздел про доставку", "добавь раздел про доставку"),
    ("добавь на сайт раздел с отзывами", "добавь раздел с отзывами"),
    ("на сайте поменяй цвет на синий", "поменяй цвет на синий"),
    ("Джарвис, переведи сайт на английский", "переведи на английский"),
])
def test_revision_phrases(phrase, what):
    assert parse_revision(phrase) == what


def test_not_revisions():
    for phrase in ("открой сайт", "сделай громче", "покажи сайт ютуба"):
        assert parse_revision(phrase) is None


def test_direction_guess():
    assert guess_direction("сайт для салона красоты") == "elegant"
    assert guess_direction("кофейня у метро") == "warm"
    assert guess_direction("магазин протеина") == "bold"
    assert guess_direction("b2b сервис автоматизации") == "tech"
    assert guess_direction("детский клуб") == "playful"


def test_match_official_site():
    results = [SearchResult("More Nutrition — Википедия", "https://de.wikipedia.org/wiki/More"),
               SearchResult("MORE Nutrition Shop", "https://www.morenutrition.de/"),
               SearchResult("Отзывы", "https://otzovik.com/more")]
    assert match_site("More Nutrition", results) == "https://morenutrition.de/"
    assert match_site("Мор Нутришн", results) == "https://morenutrition.de/"  # голосом, кириллицей
    assert match_site("Совсем другое", results) == ""


# --- описание сайта и честность -------------------------------------------------------------
SOURCES = "Протеин Total Protein 29,90 € телефон +49 30 1234567 info@more.de Берлин, Хауптштрассе 5"


def test_normalize_drops_invented_facts():
    raw = {
        "brand": "More", "hero": {"title": "Больше от каждого дня", "subtitle": "Нас выбрали 10 000 клиентов. Вкусно и просто."},
        "products": [{"name": "Total Protein", "text": "Белок.", "price": "29,90 €", "image": "https://evil.example/x.png"},
                     {"name": "Bar", "text": "Перекус.", "price": "3,50 €"}],
        "contacts": {"phone": "+7 999 000-00-00", "email": "info@more.de", "address": "Москва, Тверская 1"},
        "style": {"direction": "neon", "primary": "red", "accent": "#ffb3c1", "background": "dark"},
        "faq": [{"q": "С какого года вы работаете?", "a": "С 2009 года."}, {"q": "Есть доставка?", "a": "Да."}],
    }
    base = {"brand": "More", "hero": {}, "products": [], "contacts": {}, "style": {"direction": "bold", "primary": "#e3001b"},
            "images": {"hero": "https://more.de/hero.jpg"}}
    spec = normalize_spec(raw, base, SOURCES, images={"https://more.de/p.jpg"})
    assert spec["hero"]["subtitle"] == "Вкусно и просто."            # «10 000 клиентов» выдуманы
    assert [p["price"] for p in spec["products"]] == ["29,90 €", ""]  # цены только с сайта
    assert spec["products"][0]["image"] == ""                         # чужая картинка не попадает
    assert spec["contacts"]["phone"] == ""                            # телефон выдуман
    assert spec["contacts"]["email"] == "info@more.de"
    assert spec["contacts"]["address"] == ""                          # адрес не с сайта
    assert spec["style"] == {"direction": "bold", "primary": "#e3001b", "accent": "#ffb3c1", "background": "dark"}
    assert spec["faq"] == [{"q": "С какого года вы работаете?", "a": ""}, {"q": "Есть доставка?", "a": "Да."}]
    assert spec["images"] == {"hero": "https://more.de/hero.jpg"}


def test_normalize_strips_markup_and_limits():
    spec = normalize_spec({"hero": {"title": "<b>Жирно</b> **очень** " + "слово " * 40}}, {"hero": {}}, "")
    assert "<" not in spec["hero"]["title"] and "*" not in spec["hero"]["title"]
    assert len(spec["hero"]["title"]) <= 91


def test_extract_json_from_fenced_answer():
    assert extract_json('Вот:\n```json\n{"brand": "X"}\n```') == {"brand": "X"}
    with pytest.raises(ValueError):
        extract_json("не знаю")


def test_quick_edits_without_model():
    spec = {"style": {"direction": "bold", "primary": "#e3001b", "background": "light"}}
    dark, what = quick_edit(spec, "сделай тёмную тему")
    assert dark["style"]["background"] == "dark" and what == "тёмная тема"
    assert spec["style"]["background"] == "light"  # исходник не меняется
    assert quick_edit(spec, "сделай в элегантном стиле")[0]["style"]["direction"] == "elegant"
    assert quick_edit(spec, "поменяй цвет на синий")[0]["style"]["primary"] == "#2454d6"
    assert quick_edit(spec, "добавь раздел про доставку") is None
    assert quick_edit(spec, "перепиши заголовок") is None


# --- вёрстка ---------------------------------------------------------------
def full_spec(**over):
    spec = base_spec(parse_site_request("сделай сайт для кофейни Зерно"), None)
    spec.update({"description": "Кофейня Зерно: кофе с собой у метро, свежая выпечка каждое утро.",
                 "products": [{"name": "Капучино", "text": "", "price": "250 ₽", "tag": "Хит", "image": ""},
                              {"name": "Круассан", "text": "", "price": "180 ₽", "tag": "", "image": ""}]})
    spec.update(over)
    return spec


def test_render_basics_and_escaping():
    page = render(full_spec(brand='Зерно <script>alert(1)</script>'))
    assert page.count("<h1") == 1
    assert '<html lang="ru">' in page and 'name="viewport"' in page and 'name="description"' in page
    assert "<script>alert(1)</script>" not in page
    assert 'id="cartBtn"' in page and '"price": 250.0' in page  # все цены есть — корзина работает


def test_render_without_prices_has_order_buttons_not_cart():
    page = render(full_spec(products=[{"name": "Стрижка", "text": "", "price": "", "tag": "", "image": ""},
                                      {"name": "Борода", "text": "", "price": "", "tag": "", "image": ""}]))
    assert 'id="cartBtn"' not in page and 'data-pick="Стрижка"' in page


def test_render_ignores_bad_urls_and_colors():
    page = render(full_spec(images={"hero": "javascript:alert(1)", "logo": ""},
                            style={"direction": "x", "primary": "url(evil)", "background": "dark"}))
    assert "javascript:" not in page and "url(evil)" not in page


@pytest.mark.parametrize("raw, value, cur", [
    ("2 490 ₽", 2490.0, "₽"), ("от 1500 руб.", 1500.0, "₽"), ("29,90 €", 29.9, "€"),
    ("$12.50", 12.5, "$"), ("1.490 €", 1490.0, "€"), ("бесплатно", None, ""),
])
def test_parse_price(raw, value, cur):
    assert parse_price(raw) == (value, cur)


def test_palette_is_readable():
    for primary in ("#ffe600", "#ffffff", "#e3001b", "#0a0a0a"):
        for bg in ("light", "dark"):
            p = palette({"primary": primary, "background": bg})
            assert contrast(p["text"], p["bg"]) >= 7
            assert contrast(p["ink"], p["bg"]) >= 4.5
            assert contrast(p["on-primary"], p["primary"]) >= 3


# --- оценки качества ---------------------------------------------------------------
GOOD = {"title": "Кофейня Зерно — кофе с собой", "description": "x" * 80, "lang": "ru", "viewport": True,
        "h1": 1, "og": True, "images": 4, "imagesNoAlt": 0, "controls": 20, "controlsUnnamed": 0,
        "inputs": 3, "inputsUnlabelled": 0, "bytes": 400_000, "requests": 12}
GOOD_MOBILE = {"overflow": False, "bodyFont": 16, "smallTaps": 0}


def test_scores_perfect_and_broken():
    good = site_audit.score(GOOD, GOOD_MOBILE)
    assert {k: v["score"] for k, v in good.items()} == {"seo": 100, "mobile": 100, "a11y": 100, "speed": 100}
    bad = site_audit.score({**GOOD, "h1": 3, "description": "", "imagesNoAlt": 4, "bytes": 9_000_000,
                            "requests": 200, "inputsUnlabelled": 2},
                           {"overflow": True, "bodyFont": 12, "smallTaps": 9})
    assert bad["seo"]["score"] == 50 and bad["mobile"]["score"] == 25 and bad["speed"]["score"] == 25
    assert bad["a11y"]["score"] == 50


def test_brand_colors_from_page():
    analysis = {"colors": [{"c": "rgb(255, 255, 255)", "w": 90}, {"c": "rgb(227, 0, 27)", "w": 40},
                           {"c": "rgba(0, 0, 0, 0.1)", "w": 30}, {"c": "rgb(230, 5, 30)", "w": 10},
                           {"c": "rgb(255, 179, 193)", "w": 8}]}
    assert site_audit.brand_colors(analysis) == ("#e3001b", "#ffb3c1")
    assert site_audit.brand_colors({"colors": [{"c": "rgb(20, 20, 20)", "w": 1}]}) == (None, None)


# --- студия целиком (браузер и модель подменены) ------------------------------------------------
def fake_inspect(calls):
    def inspect(browser, url, shots_dir, prefix, analyze=False, local=False):
        calls.append((url, analyze, local))
        if local:
            return {"url": url, "desktop": GOOD, "mobile": GOOD_MOBILE,
                    "shots": {"desktop": f"{prefix}-d.png", "mobile": f"{prefix}-m.png"}}
        return {"url": url, "desktop": {**GOOD, "h1": 0, "description": ""}, "mobile": {"overflow": True},
                "shots": {"desktop": f"{prefix}-d.png", "mobile": f"{prefix}-m.png"},
                "analysis": {"url": url, "title": "MORE Nutrition", "description": "Protein & more",
                             "products": [{"name": "Total Protein", "price": "29,90 €", "image": "https://more.de/p.jpg"}],
                             "images": [{"src": "https://more.de/p.jpg", "w": 800, "h": 800}],
                             "colors": [{"c": "rgb(227, 0, 27)", "w": 10}], "tel": [], "mail": []}}
    return inspect


SPEC_ANSWER = json.dumps({
    "brand": "More Nutrition", "hero": {"title": "Больше от каждого дня", "subtitle": "Протеин и витамины.",
                                        "cta": "В каталог"},
    "products": [{"name": "Total Protein", "text": "Белок после тренировки.", "price": "29,90 €"},
                 {"name": "Creatine", "text": "Сила.", "price": "19,90 €"}],
    "benefits": [{"title": "Вкусно", "text": "Пьётся легко."}], "steps": [{"title": "Выберите", "text": "Цель."}],
    "style": {"direction": "bold", "primary": "#e3001b", "accent": "#ffb3c1", "background": "light"},
}, ensure_ascii=False)


class FakeSearch:
    def __init__(self):
        self.queries = []

    def search(self, query, limit=6):
        self.queries.append(query)
        return "duckduckgo", [SearchResult("MORE", "https://www.morenutrition.de/")]


def make_studio(tmp_path, answers, search=None):
    events, calls, opened, prompts = [], [], [], []
    answers = list(answers)

    def writer(system, user):
        prompts.append(user)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, "Тестовая модель"

    studio = SiteStudio(tmp_path / "sites", tmp_path / "shots", browser=object(), search=search,
                        writer=writer, emit=events.append)
    studio.inspect = fake_inspect(calls)
    studio.opener = lambda path: opened.append(path)
    return studio, events, calls, opened, prompts


def test_build_like_in_the_video(tmp_path):
    search = FakeSearch()
    studio, events, calls, opened, prompts = make_studio(tmp_path, [SPEC_ANSWER], search)
    said = []
    out = studio.build(parse_site_request("сделай новый сайт для More Nutrition за три часа"), said.append, lambda: False)
    assert out.ok
    assert said[0].startswith("За три часа? Сэр, это почти оскорбление")
    assert search.queries == ["More Nutrition официальный сайт"]
    assert calls[0] == ("https://morenutrition.de/", True, False)       # старый сайт — с разбором
    assert calls[1][2] is True and calls[1][0].startswith("file:")       # новый — локальный файл
    index = tmp_path / "sites" / "more-nutrition" / "index.html"
    assert opened == [index] and index.is_file()
    page = index.read_text(encoding="utf-8")
    assert ">Больше</span>" in page and "19,90 €" not in page   # выдуманная цена убрана
    assert "https://more.de/p.jpg" in page                              # картинка товара со старого сайта
    assert "Total Protein — 29,90 €" in prompts[0]                      # модель видела данные сайта
    assert "SEO — было 67, стало 100" in out.text and "И на это вы давали мне три часа?" in out.text
    project = out.data["project"]
    assert [s["status"] for s in project["steps"]] == ["ok"] * 6
    assert project["result"]["rows"][0] == {"key": "seo", "label": "SEO", "before": 67, "after": 100}
    assert any(p["say"] == "измени сайт: сделай тёмную тему" for p in project["proposals"])
    assert [e["name"] for e in events if e["type"] == "tool_start"] == [
        "site_find", "site_analyze", "site_design", "site_layout", "site_check", "site_show"]
    saved = json.loads((index.parent / "project.json").read_text(encoding="utf-8"))
    assert saved["url"] == "https://morenutrition.de/" and saved["model"] == "Тестовая модель"


def test_build_without_model_answer_uses_template(tmp_path):
    studio, events, calls, opened, prompts = make_studio(tmp_path, [RuntimeError("нет связи"), SPEC_ANSWER])
    out = studio.build(parse_site_request("сделай сайт для пекарни «Колос»"), lambda s: None, lambda: False)
    assert out.ok and calls == [(calls[0][0], False, True)]              # старого сайта нет — только проверка нового
    assert len(prompts) == 1                                             # недоступную модель второй раз не ждём
    project = out.data["project"]
    assert project["steps"][0]["status"] == "skip"
    assert "шаблон" in project["steps"][1]["detail"]
    assert "тексты пока по шаблону" in out.text                          # честно говорим, что модель не ответила
    page = (tmp_path / "sites" / "kolos" / "index.html").read_text(encoding="utf-8")
    assert "<h1" in page and "Колос" in page


def test_build_retries_when_model_answers_not_json(tmp_path):
    studio, events, calls, opened, prompts = make_studio(tmp_path, ["Конечно! Вот сайт.", SPEC_ANSWER])
    out = studio.build(parse_site_request("сделай сайт для пекарни «Колос»"), lambda s: None, lambda: False)
    assert len(prompts) == 2 and "только JSON" in prompts[1]
    assert out.data["project"]["steps"][1]["detail"].startswith("Тестовая модель")
    assert "по шаблону" not in out.text


def test_build_says_when_site_not_found(tmp_path):
    class NoSearch:
        def search(self, query, limit=6):
            return "duckduckgo", []
    studio, *_ = make_studio(tmp_path, [SPEC_ANSWER], NoSearch())
    said = []
    out = studio.build(parse_site_request("сделай сайт для пекарни «Колос»"), said.append, lambda: False)
    assert out.ok and "Официальный сайт не нашёл. Делаю с нуля." in said


def test_build_asks_for_whom(tmp_path):
    studio, *_ = make_studio(tmp_path, [])
    out = studio.build(parse_site_request("сделай сайт"), lambda s: None, lambda: False)
    assert not out.ok and "Для кого" in out.text


def test_build_cancel(tmp_path):
    studio, *_ = make_studio(tmp_path, [SPEC_ANSWER])
    out = studio.build(parse_site_request("сделай сайт для пекарни Колос"), lambda s: None, lambda: True)
    assert not out.ok and "Остановил" in out.text
    assert not list((tmp_path / "sites").glob("*/index.html"))


def test_revise_quick_and_with_model(tmp_path):
    edited = json.loads(SPEC_ANSWER)
    edited["hero"]["title"] = "Доставка за час"
    studio, events, calls, opened, prompts = make_studio(tmp_path, [SPEC_ANSWER, json.dumps(edited, ensure_ascii=False)])
    studio.build(parse_site_request("сделай сайт для пекарни Колос"), lambda s: None, lambda: False)
    folder = studio.last()
    out = studio.revise("сделай тёмную тему", lambda s: None, lambda: False)
    assert out.ok and "тёмная тема" in out.text
    assert json.loads((folder / "spec.json").read_text(encoding="utf-8"))["style"]["background"] == "dark"
    assert len(prompts) == 1                                            # тема — без модели
    out = studio.revise("перепиши заголовок про доставку за час", lambda s: None, lambda: False)
    assert out.ok
    assert "Просьба клиента: перепиши заголовок" in prompts[1]
    # «час» без цифр — можно; старая версия сохранена в history
    assert json.loads((folder / "spec.json").read_text(encoding="utf-8"))["hero"]["title"] == "Доставка за час"
    assert ">Доставка</span>" in (folder / "index.html").read_text(encoding="utf-8")
    assert len(list((folder / "history").glob("index-*.html"))) >= 1


def test_revise_without_sites(tmp_path):
    studio, *_ = make_studio(tmp_path, [])
    out = studio.revise("сделай тёмную тему", lambda s: None, lambda: False)
    assert not out.ok and "Сайтов пока нет" in out.text


# --- в ядре помощника ---------------------------------------------------------------
class FakeStudio:
    address = "сэр"

    def __init__(self, has_site=False):
        self.has_site, self.built, self.revised = has_site, [], []

    def last(self):
        return "folder" if self.has_site else None

    def build(self, req, say, cancelled):
        from jarvis.site_studio import Outcome
        say("Принято.")
        self.built.append(req)
        return Outcome("Готово, сэр.", {"kind": "site", "project": {"id": "x"}})

    def revise(self, instruction, say, cancelled):
        from jarvis.site_studio import Outcome
        self.revised.append(instruction)
        return Outcome("Поправил.")

    def open_last(self):
        return "Открываю сайт."

    def open_folder(self):
        return "Открыл папку."


def assistant_with(config, studio):
    a = Assistant(config, FakeLLM([text("ответ модели")]), ToolRegistry(), Memory(config.path("memory.db_path")))
    a.sites = studio
    return a


def test_assistant_routes_site_commands(config):
    studio = FakeStudio()
    a = assistant_with(config, studio)
    spoken = []
    reply = a.handle("Джарвис, сделай новый сайт для кофейни Зерно", on_sentence=lambda s: spoken.append(s) or True)
    assert reply.text == "Готово, сэр." and reply.data["kind"] == "site"
    assert studio.built[0].target == "кофейни Зерно" and spoken == ["Принято."]
    assert a.handle("открой сайт ютуба").text != "Открываю сайт."    # не перехватываем чужие команды
    assert a.handle("покажи сайт").text == "Открываю сайт."
    assert a.handle("открой папку с сайтом").text == "Открыл папку."


def test_assistant_revisions_only_when_site_exists(config):
    a = assistant_with(config, FakeStudio(has_site=False))
    assert a.handle("измени сайт: сделай тёмную тему").text == "ответ модели"
    studio = FakeStudio(has_site=True)
    a = assistant_with(config, studio)
    assert a.handle("измени сайт: сделай тёмную тему").text == "Поправил."
    assert a.handle("сделай сайт темнее").text == "Поправил."
    assert studio.revised == ["сделай тёмную тему", "сделай сайт темнее"]
    assert a.handle("сделай сайт для барбершопа Бритва").text == "Готово, сэр."


def test_write_structured_uses_local_model(config):
    a = Assistant(config, FakeLLM([text('{"brand": "X"}')]), ToolRegistry(), Memory(config.path("memory.db_path")))
    answer, model = a.write_structured("система", "бриф")
    assert answer == '{"brand": "X"}' and model.startswith("Ollama")
    request = a.llm.requests[0]
    assert request["options"]["num_predict"] >= 2000
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert request["stream"] is True                        # потоком: таймаут между кусочками, не на весь ответ
    a.llm.responses.append(text('{"brand": "Длинный ответ"}'))
    a.emergency.trigger()
    assert a.write_structured("система", "бриф")[0] == '{"bra'   # «стоп» прерывает генерацию


def test_site_page_has_no_model_code(tmp_path):
    """Модель не может вставить разметку или скрипт: всё её — только текст."""
    evil = json.loads(SPEC_ANSWER)
    evil["hero"]["title"] = '<img src=x onerror=alert(1)>Привет'
    evil["products"][0]["name"] = '"><script>alert(2)</script>'
    studio, *_ = make_studio(tmp_path, [json.dumps(evil)])
    studio.build(parse_site_request("сделай сайт для пекарни Колос"), lambda s: None, lambda: False)
    page = (studio.last() / "index.html").read_text(encoding="utf-8")
    assert "onerror=alert" not in page and "<script>alert" not in page
    assert len(re.findall(r"<script", page)) <= 3  # класс js, данные корзины, код страницы
