"""
Поиск по базе знаний: BM25 + лёгкий русский стеммер, без внешних зависимостей.

Почему не эмбеддинги: корпус маленький (десятки статей), запросы -
короткие технические формулировки с редкими терминами (VPN, POST, 2FA). На
таком профиле лексический поиск устойчивее и объяснимее: агент возвращает
пользователю article_id, и по нему всегда можно проверить, откуда взялся ответ.
"""

from __future__ import annotations

import math
import pathlib
import re
from collections import Counter
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "data" / "kb"

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# Служебные + шумовые слова для тикетов поддержки
STOP = {
    "и", "в", "во", "не", "на", "с", "со", "что", "как", "а", "то", "все", "она",
    "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "ее",
    "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь",
    "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть",
    "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там",
    "потом", "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо",
    "для", "при", "это", "этот", "the", "a", "of", "to", "is", "in",
    "прошу", "пожалуйста", "здравствуйте", "добрый", "день", "подскажите", "нужно",
}

# Грубое отсечение окончаний
_SUFFIXES = (
    "ированием", "ированию", "ирования", "ованиями", "ованием", "ования",
    "ениями", "ению", "ения", "ениям", "иями", "ями", "ами", "ыми", "ими",
    "ого", "его", "ому", "ему", "ые", "ий", "ая", "ое", "ые", "ой", "ей",
    "ов", "ев", "ам", "ям", "ах", "ях", "ию", "ии", "ие", "ья", "ью",
    "ет", "ут", "ют", "ат", "ят", "ла", "ло", "ли", "ся", "сь",
    "а", "я", "о", "е", "у", "ю", "ы", "и", "ь",
)


def stem(word: str) -> str:
    """Задача функции - сведение окончаний слов к одному основанию."""

    w = word.lower().replace("ё", "е")
    if len(w) <= 4 or not re.fullmatch(r"[а-я]+", w):
        return w

    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]

    return w


def tokenise(text: str) -> list[str]:
    """Простой токенизатор."""
    return [stem(t) for t in _TOKEN_RE.findall(text.lower()) if t.lower() not in STOP]


@dataclass
class Article:
    """Контейнер данных, представляющий одну статью из базы знаний в памяти системы."""

    id: str
    title: str
    category: str
    keywords: list[str]
    status: str
    body: str
    path: pathlib.Path
    tokens: list[str] = field(default_factory=list)

    def excerpt(self, n: int = 400) -> str:
        """Создаёт короткое превью текста статьи, не обрывая слова на полуслове."""

        clean = self.body.strip()
        return clean if len(clean) <= n else clean[:n].rsplit(" ", 1)[0] + "…"


def _parse(path: pathlib.Path) -> Article:
    """
    Парсер, который берёт сырой текстовый файл с жесткого диска и превращает
    его в объект Article, понятный ИИ-агенту.
    """

    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw

    # Если внутри статьи имеется разделение на уровни, сопровождающееся ---
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()

    kw = [k.strip() for k in meta.get("keywords", "").strip("[]").split(",") if k.strip()]

    art = Article(
        id=meta.get("id", path.stem),
        title=meta.get("title", path.stem),
        category=meta.get("category", ""),
        keywords=kw,
        status=meta.get("status", "published"),
        body=body,
        path=path,
    )
    # Заголовок и ключевые слова весят больше тела - дублируем их в поток токенов
    art.tokens = (
        tokenise(art.title) * 3
        + tokenise(" ".join(art.keywords)) * 3
        + tokenise(art.body)
    )

    return art


class KnowledgeBase:
    """
    BM25-индекс.

    Пересобирается на каждом обращении: корпус мал, зато всегда свежий
    после того, как агент дописал новую статью (self-learning loop).
    """

    # K1 (стандарт от 1.2 до 2.0): коэффициент насыщения частоты.
    # Определяет, насколько важно многократное повторение искомого слова в статье.
    # Если K1 высокий, статья со словом, повторившимся 10 раз получит сильно больший вес,
    # чем статья с 1 упоминанием. При низком K1 разница быстро "насыщается" и сглаживается.
    K1 = 1.5

    # B (стандарт 0.75): коэффициент нормализации по длине.
    # Штрафует длинные документы. Если B = 1.0, алгоритм сильно штрафует длинные статьи.
    # Если B = 0.0, длина статьи вообще не учитывается.
    B = 0.75

    def __init__(self, kb_dir: pathlib.Path = KB_DIR) -> None:
        self.dir = kb_dir
        self.articles = [_parse(p) for p in sorted(kb_dir.glob("*.md"))]
        self._build()

    def _build(self) -> None:
        self.N = len(self.articles) or 1
        self.avgdl = sum(len(a.tokens) for a in self.articles) / self.N or 1.0
        df: Counter[str] = Counter()
        for a in self.articles:
            df.update(set(a.tokens))
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }
        self.tf = [Counter(a.tokens) for a in self.articles]

    def search(
        self, query: str, top_k: int = 3, category: str | None = None,
        include_drafts: bool = True,
    ) -> list[tuple[Article, float]]:
        """
        Функция, которую вызывает агент.
        Берёт запрос пользователя и пропускает его через стеммер.
        """

        q = tokenise(query)
        if not q:
            return []
        scored: list[tuple[Article, float]] = []
        for art, tf in zip(self.articles, self.tf):
            if category and art.category != category:
                continue
            if not include_drafts and art.status != "published":
                continue
            dl = len(art.tokens) or 1
            score = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = self.idf.get(term, 0.0)
                score += idf * (f * (self.K1 + 1)) / (
                    f + self.K1 * (1 - self.B + self.B * dl / self.avgdl)
                )
            if score > 0:
                scored.append((art, round(score, 3)))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def get(self, article_id: str) -> Article | None:
        """Найти и вернуть конкретную статью из памяти по её ID (без учета регистра)."""
        return next((a for a in self.articles if a.id.lower() ==
                     article_id.lower()), None)

    def next_id(self) -> str:
        """Сгенерировать следующий свободный номер для создания новой статьи агентом."""

        nums = [int(m.group(1)) for a in self.articles if (m := re.search(r"(\d+)", a.id))]
        return f"KB-{max(nums, default=0) + 1:03d}"


def normalised_confidence(scores: list[float]) -> float:
    """Переводит сырой BM25-скор в [0,1] через отрыв топ-1 от топ-2.

    Абсолютное значение BM25 несопоставимо между запросами, а вот разрыв между
    первым и вторым кандидатом - рабочий сигнал уверенности: если статьи №1 и №2
    набрали почти одинаково, значит попадание неуверенное.
    """

    if not scores:
        return 0.0

    top = scores[0]
    # Ручное занижение уверенности
    if top < 1.5:
        return round(min(top / 3.0, 0.4), 2)

    runner = scores[1] if len(scores) > 1 else 0.0
    margin = (top - runner) / top
    base = min(top / 8.0, 1.0)

    return round(min(0.45 + 0.55 * (0.5 * base + 0.5 * margin), 0.99), 2)