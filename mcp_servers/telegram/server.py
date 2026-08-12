"""
MCP-сервер «Telegram» - Сценарий Б: эскалация на вторую линию.

Формат эскалации: короткая выжимка проблемы без «воды» + ссылка на тикет + тег
нужного администратора. Специалист подбирается по policies/routing.yaml,
а не выбирается моделью наугад.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from mcp.server.fastmcp import FastMCP

from common.store import ROOT, TicketStore, audit, now

mcp = FastMCP("telegram")
store = TicketStore()
MESSAGES = ROOT / "data" / "telegram" / "messages.json"
ROUTING = yaml.safe_load((ROOT / "policies" / "routing.yaml").read_text(encoding="utf-8"))
# Настраиваемое ограничение по длине выжимки агентом
MAX_SUMMARY_CHARS = 400
# Адрес карточки заявки в service desk. Вынесен в константу: при подключении
# реальной системы меняется здесь одной строкой.
TICKET_URL_TEMPLATE = "https://servicedesk.internal/tickets/{ticket_id}"


def _load() -> list[dict]:
    """Загрузка data/telegram/messages.json."""
    return json.loads(MESSAGES.read_text(encoding="utf-8")) if MESSAGES.exists() else []


def _save(items: list[dict]) -> None:
    """Сохранение в data/telegram/messages.json."""

    MESSAGES.parent.mkdir(parents=True, exist_ok=True)
    MESSAGES.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@mcp.tool()
def resolve_oncall(category: str) -> dict:
    """Ответственный специалист по этой категории. Вызывать перед escalate_to_l2."""

    cfg = ROUTING["l2_oncall"].get(category, ROUTING["l2_oncall"]["default"])

    return {"category": category, **cfg,
            "sla_minutes": ROUTING["sla_minutes"]}


@mcp.tool()
def escalate_to_l2(ticket_id: str, summary: str, impact_scope: str,
                   checks_already_done: list[str], priority: str) -> dict:
    """
    Эскалировать инцидент дежурному L2 в Telegram.

    summary - сухая техническая выжимка до 400 символов: что сломано, где, с какого
    момента. Без пересказа эмоций пользователя.
    checks_already_done - что L1 уже проверил, чтобы L2 не повторял ту же диагностику.
    """

    t = store.get(ticket_id)
    if not t:
        return {"error": f"Тикет {ticket_id} не найден"}
    if len(summary) > MAX_SUMMARY_CHARS:
        return {"error": f"Выжимка длиннее {MAX_SUMMARY_CHARS} символов - сократи."}
    if not checks_already_done:
        return {"error": "Укажи, что уже проверено, чтобы L2 не начинал диагностику с нуля"}

    cfg = ROUTING["l2_oncall"].get(t["category"], ROUTING["l2_oncall"]["default"])
    sla = ROUTING["sla_minutes"].get(priority, 240)
    checks = "\n".join(f"  • {c}" for c in checks_already_done)
    # Шаблон без отступа: это текст сообщения, отступы уехали бы в чат как есть.
    text = f"""{cfg['handle']} эскалация L1 → L2

Тикет #{ticket_id} | {t['category']} | приоритет {priority} (SLA {sla} мин)
Заявитель: {t['user']}

Суть: {summary}
Охват: {impact_scope}

Уже проверено на L1:
{checks}

Ссылка: {TICKET_URL_TEMPLATE.format(ticket_id=ticket_id)}"""

    items = _load()
    msg = {"id": len(items) + 1,
           "chat": "l2-duty",
           "to": cfg["handle"],
           "ticket_id": str(ticket_id),
           "text": text,
           "sent_at": now(),
           "priority": priority,
           "sla_minutes": sla,
           "status": "sent"
          }
    items.append(msg)
    _save(items)

    store.annotate(ticket_id, "escalation", {"to": cfg["handle"],
                                             "message_id": msg["id"],
                                             "sla_minutes": sla})
    store.patch(ticket_id, {"status": "В работе"}, actor="l1-agent",
                reason=f"Эскалация на L2 {cfg['handle']}")
    audit("telegram.escalate", ticket_id=ticket_id, to=cfg["handle"], priority=priority)

    return {"ok": True, "sent_to": cfg["handle"], "engineer": cfg["name"],
            "sla_minutes": sla, "message_preview": text}


@mcp.tool()
def notify_user_pending(ticket_id: str, eta_minutes: int) -> dict:
    """Предупредить заявителя, что инцидент передан профильному инженеру."""

    store.annotate(ticket_id, "user_notified", {"eta_minutes": eta_minutes, "at": now()})

    return {"ok": True, "ticket_id": ticket_id, "eta_minutes": eta_minutes}


@mcp.tool()
def get_l2_response(ticket_id: str) -> dict:
    """
    Получить ответ инженера L2 по тикету (макет для демонстрации самообучения).

    В реальной интеграции здесь читается тред Telegram. В макете ответы лежат
    в data/telegram/l2_responses.json и подставляются для демо-сценария:
    ответ L2 -> новая статья в базе знаний -> следующий похожий тикет
    закрывается по Сценарию А.
    """

    path = ROOT / "data" / "telegram" / "l2_responses.json"
    responses = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    r = responses.get(str(ticket_id))
    if not r:
        return {"pending": True, "note": "Ответ L2 пока не получен"}

    return {"ticket_id": str(ticket_id),
            "responded_by": r["engineer"],
            "resolution": r["resolution"],
            "root_cause": r.get("root_cause", ""),
            "next_step": "Оформи решение статьёй через kb_create_article"
           }


@mcp.tool()
def list_escalations() -> dict:
    """Все отправленные эскалации."""

    items = _load()

    return {"count": len(items), "escalations": [
        {"id": m["id"], "ticket_id": m["ticket_id"], "to": m["to"],
         "priority": m["priority"], "sent_at": m["sent_at"]} for m in items]}


if __name__ == "__main__":
    mcp.run()