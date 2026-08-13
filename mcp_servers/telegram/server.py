"""
MCP-сервер «Telegram» - Сценарий Б: эскалация на вторую линию.

Формат эскалации: короткая выжимка проблемы без «воды» + ссылка на тикет + тег
нужного администратора. Специалист подбирается по policies/routing.yaml,
а не выбирается моделью наугад.

Канал работает в двух режимах:

  макет (по умолчанию)   сообщения складываются в data/telegram/messages.json,
                         ответы инженеров берутся из фикстуры. Прогон
                         воспроизводится у любого без токенов и сети.

  реальный Telegram      включается, когда заданы TELEGRAM_BOT_TOKEN и
                         TELEGRAM_CHAT_ID. Эскалация уходит настоящим
                         сообщением, ответ инженера подхватывается через
                         reply на это сообщение.

Разделение то же, что у LIVE_WRITES в common/store.py: демо обязано
воспроизводиться без внешних сервисов, а реальная интеграция включается флагом.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from mcp.server.fastmcp import FastMCP

from common.env import load_env
from common.store import ROOT, TicketStore, audit, now

load_env()

mcp = FastMCP("telegram")
store = TicketStore()
MESSAGES = ROOT / "data" / "telegram" / "messages.json"
INBOX = ROOT / "data" / "telegram" / "inbox.json"
ROUTING = yaml.safe_load((ROOT / "policies" / "routing.yaml").read_text(encoding="utf-8"))
# Настраиваемое ограничение по длине выжимки агентом
MAX_SUMMARY_CHARS = 400
# Адрес карточки заявки в service desk. Вынесен в константу: при подключении
# реальной системы меняется здесь одной строкой.
TICKET_URL_TEMPLATE = "https://servicedesk.internal/tickets/{ticket_id}"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LIVE = bool(BOT_TOKEN and CHAT_ID)
API_TIMEOUT = 15


def _load() -> list[dict]:
    """Загрузка data/telegram/messages.json."""
    return json.loads(MESSAGES.read_text(encoding="utf-8")) if MESSAGES.exists() else []


def _save(items: list[dict]) -> None:
    """Сохранение в data/telegram/messages.json."""

    MESSAGES.parent.mkdir(parents=True, exist_ok=True)
    MESSAGES.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# Дальше код реального канала TG

def _api(method: str, params: dict) -> dict | None:
    """Вызов Bot API. Сбой не должен ронять эскалацию - только пишем в журнал."""

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()

    try:
        with urllib.request.urlopen(url, data=data, timeout=API_TIMEOUT) as response:
            payload = json.loads(response.read())
        return payload.get("result") if payload.get("ok") else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        audit("telegram.api_failed", method=method, error=str(exc)[:200])
        return None


def _send(text: str) -> int | None:
    """Отправить сообщение администратору. Возвращает message_id для связи с ответом."""

    result = _api("sendMessage", {"chat_id": CHAT_ID, "text": text})
    return result.get("message_id") if result else None


def _read_inbox() -> dict:

    if INBOX.exists():
        return json.loads(INBOX.read_text(encoding="utf-8"))
    
    return {"offset": 0, "messages": []}


def _poll() -> dict:
    """
    Забрать новые сообщения из Telegram и дописать их в локальный inbox.

    getUpdates отдаёт каждое обновление один раз, поэтому складываем всё в файл
    и отвечаем уже из него: повторный вызов инструмента не теряет ответ инженера.
    """

    inbox = _read_inbox()
    result = _api("getUpdates", {"offset": inbox["offset"] + 1, "timeout": 0})
    if not result:
        return inbox

    for update in result:
        inbox["offset"] = max(inbox["offset"], update["update_id"])
        message = update.get("message") or {}
        if not message.get("text"):
            continue
        sender = message.get("from", {})
        inbox["messages"].append({
            "message_id": message["message_id"],
            "reply_to": (message.get("reply_to_message") or {}).get("message_id"),
            "text": message["text"],
            "from": sender.get("username") or sender.get("first_name") or "инженер",
            "date": message.get("date"),
        })

    INBOX.parent.mkdir(parents=True, exist_ok=True)
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=2), encoding="utf-8")
    return inbox


def _find_reply(ticket_id: str, sent_message_id: int | None) -> dict | None:
    """
    Найти ответ инженера по этой заявке.

    Основной способ - reply на сообщение бота: Telegram сам приносит
    reply_to_message_id, и связь получается однозначной. Запасной - упоминание
    #номера в тексте, на случай если инженер написал ответ отдельным сообщением.
    """

    messages = _poll()["messages"]
    for message in reversed(messages):
        if sent_message_id and message.get("reply_to") == sent_message_id:
            return message
    tag = f"#{ticket_id}"
    for message in reversed(messages):
        if tag in message["text"]:
            return message
    return None


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

    # Реальная доставка. Сохраняет message_id: по ответу на это сообщение
    # инженера для связи с заявкой.
    if LIVE:
        sent_id = _send(text)
        msg["telegram_message_id"] = sent_id
        msg["delivered"] = sent_id is not None

    items.append(msg)
    _save(items)

    store.annotate(ticket_id, "escalation", {"to": cfg["handle"],
                                             "message_id": msg["id"],
                                             "sla_minutes": sla})
    store.patch(ticket_id, {"status": "В работе"}, actor="l1-agent",
                reason=f"Эскалация на L2 {cfg['handle']}")
    audit("telegram.escalate", ticket_id=ticket_id, to=cfg["handle"], priority=priority,
          channel="telegram" if LIVE else "mock", delivered=msg.get("delivered"))

    return {"ok": True, "sent_to": cfg["handle"], "engineer": cfg["name"],
            "sla_minutes": sla, "message_preview": text,
            "channel": "telegram" if LIVE else "макет (реальная отправка выключена)",
            "delivered": msg.get("delivered", False)}


@mcp.tool()
def notify_user_pending(ticket_id: str, eta_minutes: int) -> dict:
    """Предупредить заявителя, что инцидент передан профильному инженеру."""

    store.annotate(ticket_id, "user_notified", {"eta_minutes": eta_minutes, "at": now()})

    return {"ok": True, "ticket_id": ticket_id, "eta_minutes": eta_minutes}


@mcp.tool()
def get_l2_response(ticket_id: str) -> dict:
    """
    Получить ответ инженера второй линии по заявке.

    Если подключён реальный Telegram, читается ответ на сообщение об эскалации:
    инженер отвечает реплаем, и связь с заявкой получается однозначной. Иначе
    ответ берётся из фикстуры data/telegram/l2_responses.json.

    Ответа ещё нет - вернётся pending. Это не ошибка: значит, инженер пока
    не написал, и статью в базу знаний писать рано.
    """

    if LIVE:
        sent = next((m for m in reversed(_load())
                     if m["ticket_id"] == str(ticket_id)), None)
        reply = _find_reply(str(ticket_id), (sent or {}).get("telegram_message_id"))
        if reply:
            audit("telegram.l2_reply", ticket_id=ticket_id, source="telegram",
                  responder=reply["from"])
            return {"ticket_id": str(ticket_id),
                    "responded_by": f"@{reply['from']}",
                    "resolution": reply["text"],
                    "root_cause": "",
                    "source": "Telegram",
                    "next_step": "Оформи решение статьёй через kb_create_article"
                   }

    path = ROOT / "data" / "telegram" / "l2_responses.json"
    responses = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    r = responses.get(str(ticket_id))
    if not r:
        return {"pending": True,
                "note": ("Ответ L2 пока не получен. Если инженеру уже написали, "
                         "подожди и вызови инструмент ещё раз." if LIVE
                         else "Ответ L2 пока не получен")}

    return {"ticket_id": str(ticket_id),
            "responded_by": r["engineer"],
            "resolution": r["resolution"],
            "root_cause": r.get("root_cause", ""),
            "source": "фикстура",
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