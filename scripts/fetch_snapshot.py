"""
Скачивает снапшот очереди тикетов один раз, чтобы прогоны были воспроизводимыми.

Запуск:  python scripts/fetch_snapshot.py
"""

import json
import pathlib
import urllib.request

API = "https://6a7ad74c8c69b3eb4a179621.mockapi.io/tickets/tickets"
OUT = pathlib.Path(__file__).resolve().parents[1] / "data" / "cache" / "tickets_snapshot.json"

NOISE = {"name", "avatar", "createdAt"} # мусорные поля генератора mockapi


def main() -> None:

    with urllib.request.urlopen(API, timeout=30) as r:
        raw = json.load(r)

    clean = [{k: v for k, v in t.items() if k not in NOISE} for t in raw]
    clean.sort(key=lambda t: int(t["id"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: {len(clean)} тикетов -> {OUT}")
    print("Категории:", sorted({t['category'] for t in clean}))
    print("Приоритеты:", sorted({t['priority'] for t in clean}))
    print("Статусы:", sorted({t['status'] for t in clean}))


if __name__ == "__main__":
    main()