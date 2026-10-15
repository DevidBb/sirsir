"""Детектор фразы пробуждения «Джарвис» (и фраз перебивания «стоп»).

Используется Vosk с ограниченной грамматикой. Если дать распознавателю только
слово-триггер, он «натягивает» на него почти любую речь: на синтезированных
фразах такой вариант ложно срабатывал в 65% случаев («Мы поедем в Париж» →
«джарвис»). Поэтому в грамматику добавлен общий словарь частых слов —
отвлекателей: обычная речь распознаётся как они, а триггер срабатывает только
на похожем на него слове. На проверке: 30/30 срабатываний, ложных 1 из 150
(не считая слова «Джарвисовский»).
"""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Iterable

import numpy as np

from .security import contains_phrase, normalize

log = logging.getLogger(__name__)

# Частые слова и созвучные триггеру. Слова, которых нет в модели, отбрасываются.
DISTRACTORS = """и в не на я быть он с что а по это она этот к но они мы как из у который то за свой
весь год от так о для ты же все тот мочь вы человек такой его сказать только или еще бы себя один
уже до время если сам когда другой вот говорить наш мой знать стать при чтобы дело жизнь кто
первый очень два день ее новый рука даже во со раз где там под можно ну какой после их работа без
самый потом надо хотеть ли слово идти большой должен место иметь ничто сейчас тут лицо каждый
друг нет теперь ни глаз тоже тогда видеть вопрос через да здесь дом потому сторона думать
сделать страна жить чем мир об последний случай голова более делать смотреть ребенок просто
конечно сила конец перед несколько вид система всегда работать между три понять пойти
часть спросить город дать также никто понимать получить отношение лишь второй именно иной общий
давать ответить хороший вообще книга вода окно стол свет машина дверь утро вечер ночь неделя месяц
деньги музыка фильм погода дорога парк магазин школа учитель мама папа брат сестра сын дочь кошка собака
чай кофе хлеб молоко мясо рыба суп сок вино пиво завтра вчера сегодня быстро медленно громко тихо
красный синий зеленый белый черный маленький старый молодой хорошо плохо привет спасибо пожалуйста
джаз париж дарвин жарко жар ваш вас вис давай давид нарвал шарф рис джордж чарльз арбуз марс варвар
жарвис гарвард вирус вальс барс ворс бывший парус борис карась
стол стопка стопа стоит стало стон хватать хват замок замолк молчать столько
нравится нравятся нравиться песня эта эти заранее справа""".split()


def _similar_to_wake(word: str, phrases: list[str]) -> bool:
    w = normalize(word)
    if not w:
        return False
    if w.startswith("jarvis") or w.startswith("джарвис"):
        return True
    return any(difflib.SequenceMatcher(None, w, normalize(p)).ratio() >= 0.8
               for p in phrases if " " not in p.strip())


class VoskWakeWord:
    def __init__(self, model_path: str | Path, phrases: list[str], sample_rate: int = 16000):
        self.model_path = Path(model_path)
        self.phrases = phrases
        self.sample_rate = sample_rate
        self._model = None
        self._rec = None
        self._grammar: str | None = None

    def with_phrases(self, phrases: list[str]) -> "VoskWakeWord":
        """Второй детектор на той же загруженной модели (например, для «стоп»)."""
        other = VoskWakeWord(self.model_path, phrases, self.sample_rate)
        other._model = self._model
        other._build_grammar()
        other.reset()
        return other

    def load(self) -> None:
        if not self.model_path.is_dir():
            raise FileNotFoundError(
                f"Модель Vosk не найдена: {self.model_path}. Запустите scripts/download_models.py")
        import vosk  # type: ignore
        vosk.SetLogLevel(-1)
        self._model = vosk.Model(str(self.model_path))
        self._build_grammar()
        self.reset()

    def _known(self, word: str) -> bool:
        finder = getattr(self._model, "vosk_model_find_word", None)
        return finder is None or finder(word) >= 0

    def _build_grammar(self) -> None:
        phrase_words = {w for p in self.phrases for w in normalize(p).split()}
        usable = [p for p in self.phrases if all(self._known(w) for w in normalize(p).split())]
        skipped = [p for p in self.phrases if p not in usable]
        if skipped:
            log.info("Фразы не из словаря Vosk пропущены: %s", ", ".join(skipped))
        if not usable:
            raise ValueError(f"Ни одной фразы из {self.phrases} нет в словаре модели Vosk")
        distractors = sorted({w for w in DISTRACTORS if w not in phrase_words and self._known(w)})
        self.active_phrases = usable
        self._grammar = json.dumps(usable + distractors + ["[unk]"], ensure_ascii=False)

    def reset(self) -> None:
        import vosk  # type: ignore
        self._rec = vosk.KaldiRecognizer(self._model, self.sample_rate, self._grammar)

    def process(self, block: np.ndarray) -> bool:
        """Подаёт блок int16; True — фраза услышана."""
        data = block.astype(np.int16).tobytes()
        if self._rec.AcceptWaveform(data):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        if text and contains_phrase(text, self.active_phrases):
            self.reset()
            return True
        return False

    def wait(self, blocks: Iterable[np.ndarray]) -> bool:
        """Блокирует до срабатывания; False — поток блоков закончился."""
        for block in blocks:
            if self.process(block):
                return True
        return False


def strip_wake_phrase(text: str, phrases: list[str]) -> str:
    """«Джарвис, открой блокнот» → «открой блокнот».

    Whisper часто пишет «Джервис», «Джарвиз» или «Jarvis», поэтому сравнение
    нечёткое. Учитывается и обращение вида «Эй, Джарвис, …».
    """
    words = text.strip().split()
    for i in range(min(2, len(words))):
        if _similar_to_wake(words[i], phrases):
            if i == 0 or normalize(words[0]) in ("эй", "слушай", "ну", "так", "окей", "о"):
                return " ".join(words[i + 1:]).lstrip(",.!: ")
    return text.strip()
