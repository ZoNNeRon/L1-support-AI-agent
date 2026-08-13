"""
Чтение .env без внешних зависимостей.

Отдельная библиотека ради разбора двенадцати строк не нужна, а лишняя
зависимость в проекте, который заявляет «BM25 написан руками», выглядела бы
странно. Формат поддерживается минимальный: KEY=VALUE, комментарии с #,
необязательные кавычки вокруг значения.

Переменные, уже заданные в окружении, приоритетнее файла: так временный
запуск с другим токеном не требует правки .env.
"""

from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def load_env(path: pathlib.Path = ENV_FILE) -> dict[str, str]:
    """Загрузить .env в os.environ. Возвращает то, что реально положено."""

    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value

    return loaded