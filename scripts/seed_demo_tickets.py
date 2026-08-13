"""
Добавляет в снапшот очереди четыре заявки для демонстрации всех сценариев.

Заявки:

  901  Сценарий А    есть готовая инструкция, закрывается сразу
  902  Сценарий Б    инструкции нет - уходит инженеру второй линии
  903  Сценарий В    дефект программы - баг-репорт в разработку
  904  Сценарий А    то же обращение, что и 902, но уже после того,
                     как агент оформил ответ инженера статьёй

Заголовки, категории и приоритеты намеренно проставлены неверно - ровно так,
как это выглядит в реальной очереди. 

Запуск:   python scripts/seed_demo_tickets.py
Откат:    python scripts/seed_demo_tickets.py --remove
          (либо make snapshot - перекачает очередь заново)
Уборка:   python scripts/seed_demo_tickets.py --clean-drafts
          удаляет черновики статей, написанные агентом в прошлых прогонах
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "cache" / "tickets_snapshot.json"
KB_DIR = ROOT / "data" / "kb"

# Номера с запасом от реальных id очереди, чтобы ничего не перекрыть.
DEMO_IDS = {"901", "902", "903", "904"}

TELEPHONY = "Не работает служебная IP-телефония, при наборе номера тишина."

DEMO_TICKETS = [
    {
        "id": "901",
        "user": "Анна Соколова",
        "title": "Сломался принтер",                    # заголовок не про то
        "category": "Ошибки в работе ПО",               # категория неверна
        "priority": "Критический",                      # приоритет завышен
        "status": "Новая",
        "description": "Не могу подключиться к рабочему Wi-Fi с ноутбука.",
    },
    {
        "id": "902",
        "user": "Дмитрий Егоров",
        "title": "Вопрос по отчётам",
        "category": "Консультация",
        "priority": "Низкий",                           # приоритет занижен
        "status": "Новая",
        "description": TELEPHONY,
    },
    {
        "id": "903",
        "user": "Ольга Кузнецова",
        "title": "Не приходит СМС",
        "category": "Доступ и авторизация",
        "priority": "Низкий",
        "status": "Новая",
        "description": "Кнопка «Согласовать» ничего не делает, в консоли ошибка 500.",
    },
    {
        "id": "904",
        "user": "Павел Никитин",
        "title": "Проблема с телефоном",
        "category": "Оборудование",
        "priority": "Средний",
        "status": "Новая",
        # То же обращение, что и 902: после появления статьи закроется само.
        "description": TELEPHONY,
    },
]

TIMESTAMP = "2026-08-13T09:00:00"


def clean_drafts() -> int:
    """
    Удалить черновики агента, оставшиеся от прошлых прогонов.

    Удаляются только статьи, которые одновременно (1) написаны агентом и
    (2) не отслеживаются git. Второе условие принципиально: статья, попавшая
    в репозиторий, - уже часть проекта, а не мусор прогона, и стирать её
    подготовкой к записи нельзя.
    """

    try:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "data/kb"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("git недоступен - черновики не трогаю, удали вручную при необходимости")
        return 0

    removed = 0
    for name in untracked:
        path = ROOT / name
        if path.suffix == ".md" and "created_by: l1-agent" in path.read_text(encoding="utf-8"):
            path.unlink()
            print(f"  удалён черновик {path.name}")
            removed += 1

    print(f"Черновиков агента удалено: {removed}")
    return 0


def main() -> int:
    if "--clean-drafts" in sys.argv:
        return clean_drafts()

    if not SNAPSHOT.exists():
        print("Снапшот не найден. Сначала выполни: make snapshot")
        return 1

    tickets = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    kept = [t for t in tickets if t["id"] not in DEMO_IDS]
    removed = len(tickets) - len(kept)

    if "--remove" in sys.argv:
        SNAPSHOT.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Удалено демо-заявок: {removed}. В очереди осталось: {len(kept)}")
        return 0

    for ticket in DEMO_TICKETS:
        kept.append({**ticket, "created_at": TIMESTAMP, "updated_at": TIMESTAMP})
    kept.sort(key=lambda t: int(t["id"]))

    SNAPSHOT.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Добавлено демо-заявок: {len(DEMO_TICKETS)}. Всего в очереди: {len(kept)}\n")
    for ticket in DEMO_TICKETS:
        print(f"  #{ticket['id']}  «{ticket['title']}» / {ticket['category']} "
              f"/ {ticket['priority']}")
        print(f"        по тексту: {ticket['description']}")
    print("\nПорядок обработки на записи: 901 -> 902 -> (ответ инженера) -> 903 -> 904")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())