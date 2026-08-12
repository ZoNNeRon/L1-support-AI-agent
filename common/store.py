"""
Слой данных: снапшот очереди + локальный shadow-store изменений.

Почему shadow-store, а не PUT в mockapi:
  1. mockapi - публичный общий ресурс. Прогоны должны быть воспроизводимыми.
  2. Все действия агента должны быть обратимыми и полностью аудируемыми -
     это требование к автономному агенту, который «увольняет» первую линию.

Реальная запись включается флагом LIVE_WRITES=1 (адаптер оставлен в write_through()).
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "cache" / "tickets_snapshot.json"
SHADOW = ROOT / "data" / "shadow" / "overlay.json"
AUDIT = ROOT / "data" / "shadow" / "audit.jsonl"

# Заявка - обычный словарь, разобранный из JSON. Алиас нужен только для
# читаемости сигнатур: dict[str, Any] в каждой строке ничего не объясняет.
Ticket = dict[str, Any]

_LOCK = threading.Lock()


def now() -> str:
    """Текущие дата и время по UTC."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: pathlib.Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


class TicketStore:
    """Снапшот доступен только на чтение, все изменения агента пишутся в overlay."""

    def __init__(self) -> None:
        self._base: dict[str, Ticket] = {
            str(t["id"]): t for t in _read_json(SNAPSHOT, [])
        }
        if not self._base:
            raise RuntimeError(
                "Снапшот пуст. Сначала выполни: python scripts/fetch_snapshot.py"
            )

    # Команды на чтение

    @property
    def overlay(self) -> dict[str, Ticket]:
        """Открытие того самого overlay.json."""
        return _read_json(SHADOW, {})

    def get(self, ticket_id: str) -> Ticket | None:
        """
        Объединение оригинальной информации пользователя и того, что понял агент.

        Возвращает None, если заявки нет. Это рабочий сценарий, а не сбой:
        id приходит от модели, она вполне может назвать несуществующий.
        Поэтому MCP-инструменты обязаны проверить результат и вернуть
        пользователю понятную ошибку. Там, где заявка заведомо существует,
        вызывай require() - он с типом Ticket, без None.
        """

        base = self._base.get(str(ticket_id))
        if base is None:
            return None

        merged = dict(base)
        patch = self.overlay.get(str(ticket_id))
        if patch:
            merged.update({k: v for k, v in patch.items() if k != "_agent"})
            merged["_agent"] = patch.get("_agent", {})

        return merged

    def require(self, ticket_id: str) -> Ticket:
        """
        То же, что get(), но заявка обязана существовать.

        Для внутренних вызовов, где наличие заявки уже установлено (перебор
        собственных ключей, проверка в начале patch). Отсутствие здесь - не
        рабочая ситуация, а ошибка в коде.
        """

        ticket = self.get(ticket_id)
        if ticket is None:
            raise KeyError(f"Тикет {ticket_id} не найден")
        return ticket

    def list(
            self,
            status: str | None = None,
            category: str | None = None,
            processed: bool | None = None,
            limit: int = 20,
            offset: int = 0,
        ) -> list[Ticket]:
        """Конвейер подачи заявок в агента."""

        out: list[Ticket] = []

        # Перебираем собственные ключи, поэтому заявка гарантированно найдётся.
        for tid in sorted(self._base, key=int):
            t = self.require(tid)
            if status and t["status"] != status:
                continue
            if category and t["category"] != category:
                continue
            if processed is not None and bool(t.get("_agent")) != processed:
                continue
            out.append(t)

        return out[offset : offset + limit]

    def all_ids(self) -> Iterable[str]:
        """Сортировка всех айди базы по возрастанию."""
        return sorted(self._base, key=int)

    def original(self, ticket_id: str) -> Ticket:
        """
        Исходная запись из очереди - нужна для метрики «агент исправил X%».

        Как и require(), рассчитан на внутренние вызовы: сравнивать «было/стало»
        имеет смысл только для заявки, которая уже найдена.
        """

        base = self._base.get(str(ticket_id))
        if base is None:
            raise KeyError(f"Тикет {ticket_id} не найден")
        return base

    # Команды на запись

    def patch(self, ticket_id: str, changes: dict, actor: str, reason: str = "") -> Ticket:
        """Инструмент агента для безопасного внесения изменений в заявки."""

        tid = str(ticket_id)
        if tid not in self._base:
            raise KeyError(f"Тикет {tid} не найден")

        with _LOCK:
            overlay = self.overlay
            entry = overlay.setdefault(tid, {"_agent": {}})
            # Состояние до патча читаем один раз: get() каждый раз перечитывает
            # overlay.json с диска, а внутри comprehension это был бы лишний
            # проход по файлу на каждое изменяемое поле.
            current = self.require(tid)
            before = {k: current.get(k) for k in changes}
            entry.update({k: v for k, v in changes.items() if k != "_agent"})
            entry.setdefault("_agent", {}).update({"actor": actor, "updated_at": now()})
            entry["updated_at"] = now()
            SHADOW.parent.mkdir(parents=True, exist_ok=True)
            SHADOW.write_text(
                json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        audit(
            "ticket.patch",
            ticket_id=tid,
            actor=actor,
            reason=reason,
            before=before,
            after=changes,
        )

        if os.getenv("LIVE_WRITES") == "1":
            write_through(tid, changes)
        return self.require(tid)

    def annotate(self, ticket_id: str, key: str, value: Any) -> None:
        """Служебные пометки агента (решение, ссылки на KB, confidence)."""

        tid = str(ticket_id)
        with _LOCK:
            overlay = self.overlay
            entry = overlay.setdefault(tid, {"_agent": {}})
            entry.setdefault("_agent", {})[key] = value
            SHADOW.parent.mkdir(parents=True, exist_ok=True)
            SHADOW.write_text(
                json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def reset(self) -> None:
        """Физическое удаление overlay с диска."""
        SHADOW.unlink(missing_ok=True)


def write_through(ticket_id: str, changes: dict) -> None:
    """Опциональная реальная запись в mockapi (LIVE_WRITES=1). По умолчанию выключена."""

    import urllib.request

    url = f"https://6a7ad74c8c69b3eb4a179621.mockapi.io/tickets/tickets/{ticket_id}"
    req = urllib.request.Request(
        url,
        data=json.dumps(changes).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            audit("ticket.write_through", ticket_id=ticket_id, http_status=r.status)
    except Exception as exc: # noqa: BLE001
        audit("ticket.write_through_failed", ticket_id=ticket_id, error=str(exc))


def audit(event: str, **fields: Any) -> None:
    """Журнал решений только с возможностью записи. Основа трассируемости и eval-метрик."""

    AUDIT.parent.mkdir(parents=True, exist_ok=True)

    rec = {"ts": now(), "event": event, **fields}
    with _LOCK, AUDIT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_audit() -> list[dict]:
    """Общая функция чтения аудита."""

    if not AUDIT.exists():
        return []
    return [json.loads(line) for line in AUDIT.read_text(encoding="utf-8").splitlines() if line]