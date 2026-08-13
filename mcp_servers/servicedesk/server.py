"""
MCP-сервер «Service Desk» - очередь заявок.

Инструменты соответствуют реальным правам специалиста L1: прочитать очередь,
переклассифицировать, ответить пользователю, сменить статус. Права уровня L2
(перезапуск сервисов, выдача доступов) сознательно не выданы - агент должен
эскалировать, а не пытаться сделать то, чего не умеет первая линия.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from mcp.server.fastmcp import FastMCP

from common.store import ROOT, TicketStore, audit

mcp = FastMCP("servicedesk")
store = TicketStore()
TAXONOMY = yaml.safe_load((ROOT / "policies" / "taxonomy.yaml").read_text(encoding="utf-8"))
GUARDRAILS = yaml.safe_load((ROOT / "policies" / "guardrails.yaml").read_text(encoding="utf-8"))

CATEGORIES = [c["name"] for c in TAXONOMY["categories"]]
PRIORITIES = TAXONOMY["priorities"]
STATUSES = TAXONOMY["statuses"]
PATHS = list(TAXONOMY["resolution_paths"])


def _view(t: dict) -> dict:
    """
    Отдаём агенту только релевантные поля. Исходные category/priority помечены,
    чтобы модель не принимала их за истину - в очереди они проставлены случайно.
    """

    return {
        "id": t["id"],
        "user": t["user"],
        "title": t["title"],
        "description": t["description"],
        "status": t["status"],
        "category_as_reported": t["category"],
        "priority_as_reported": t["priority"],
        "created_at": t.get("created_at"),
        "agent_state": t.get("_agent", {}),
    }


@mcp.tool()
def list_tickets(status: str = "Новая", limit: int = 10, offset: int = 0,
                 only_unprocessed: bool = True) -> dict:
    """
    Вернуть очередь заявок.

    status: один из Новая / В работе / Ожидание ответа / Решена / Закрыта.
    only_unprocessed: показывать только те, которых агент ещё не касался.
    """

    if status and status not in STATUSES:
        return {"error": f"Недопустимый статус. Доступно: {STATUSES}"}

    items = store.list(
        status=status or None,
        processed=False if only_unprocessed else None,
        limit=limit,
        offset=offset,
    )

    return {"count": len(items), "tickets": [_view(t) for t in items]}


@mcp.tool()
def get_ticket(ticket_id: str) -> dict:
    """Полная карточка одной заявки."""

    t = store.get(ticket_id)

    return _view(t) if t else {"error": f"Тикет {ticket_id} не найден"}


@mcp.tool()
def classify_ticket(ticket_id: str, category: str, impact: str, urgency: str,
                    resolution_path: str, reasoning: str, confidence: float) -> dict:
    """
    Зафиксировать результат триажа.

    Приоритет НЕ передаётся напрямую - он вычисляется из impact x urgency по
    матрице policies/priority_matrix.yaml. Задача модели - оценить только эти два
    измерения и обосновать выбор.

    impact:  single_user | team | org_wide
    urgency: workaround | degraded | blocked
    resolution_path: KB_RESOLVE | ESCALATE_L2 | BUG_REPORT | NEED_INFO
    confidence: уверенность в классификации, 0.0-1.0
    """

    if category not in CATEGORIES:
        return {"error": f"Недопустимая категория. Доступно: {CATEGORIES}"}
    if resolution_path not in PATHS:
        return {"error": f"Недопустимый маршрут. Доступно: {PATHS}"}

    t = store.get(ticket_id)
    if not t:
        return {"error": f"Тикет {ticket_id} не найден"}

    priority, calc = compute_priority(impact, urgency, f"{t['title']} {t['description']}")
    if "error" in calc:
        return calc

    min_conf = GUARDRAILS["confidence"]["escalate_min"]
    if confidence < min_conf and resolution_path != "NEED_INFO":
        resolution_path = "NEED_INFO"
        reasoning += (
            f" [guardrail: confidence {confidence} < {min_conf}, "
            "маршрут понижен до NEED_INFO вместо угадывания]"
        )

    store.patch(
        ticket_id,
        {"category": category, "priority": priority, "status": "В работе"},
        actor="l1-agent",
        reason=reasoning,
    )
    store.annotate(ticket_id, "triage", {
        "impact": impact, "urgency": urgency, "score": calc["score"],
        "resolution_path": resolution_path, "confidence": confidence,
        "reasoning": reasoning,
    })

    orig = store.original(ticket_id)
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "category": category,
        "priority": priority,
        "priority_calculation": calc,
        "resolution_path": resolution_path,
        "corrected_category": orig["category"] != category,
        "corrected_priority": orig["priority"] != priority,
        "next_step": TAXONOMY["resolution_paths"][resolution_path],
    }


@mcp.tool()
def compute_priority_preview(impact: str, urgency: str, text: str = "") -> dict:
    """Посчитать приоритет по матрице, ничего не меняя в тикете."""

    priority, calc = compute_priority(impact, urgency, text)
    return {"priority": priority, **calc}


def compute_priority(impact: str, urgency: str, text: str) -> tuple[str, dict]:
    """Рассчитать приоритет (по матрице priority_matrix.yaml)."""

    m = yaml.safe_load((ROOT / "policies" / "priority_matrix.yaml").read_text(encoding="utf-8"))

    if impact not in m["impact"]:
        return "", {"error": f"impact должен быть одним из {list(m['impact'])}"}
    if urgency not in m["urgency"]:
        return "", {"error": f"urgency должен быть одним из {list(m['urgency'])}"}

    score = m["impact"][impact] * m["urgency"][urgency]
    priority = m["matrix"][score]
    calc = {"impact": impact, "urgency": urgency, "score": score, "rule": "matrix"}
    low = text.lower()

    for ov in m.get("overrides", []):
        if any(sig in low for sig in ov["if_signals"]):
            priority = ov["priority"]
            calc = {**calc, "rule": "override", "override_reason": ov["reason"]}
            break

    return priority, calc


@mcp.tool()
def reply_to_ticket(ticket_id: str, message: str, kb_article_ids: list[str] | None = None,
                    reply_kind: str = "solution",
                    close_ticket: bool = False, confidence: float = 0.0) -> dict:
    """
    Отправить ответ пользователю.

    reply_kind определяет, что это за сообщение, и от него зависят требования:

      solution       решение проблемы. Обязателен kb_article_ids: инструкция
                     должна опираться на статью базы знаний, а не на догадку
                     модели. Только такой ответ может закрыть заявку.
      status_update  уведомление о ходе работы - «дефект подтверждён и передан
                     в разработку», «инцидент у профильного инженера». Ссылка
                     на статью не нужна, потому что решения здесь нет.
                     Разрешён только когда действие реально выполнено.
      clarification  уточняющий вопрос, когда данных не хватает (маршрут
                     NEED_INFO). Ссылка не нужна, заявка остаётся открытой.

    Автозакрытие возможно только для solution и блокируется, если уверенность
    ниже порога - тикет останется в статусе «Ожидание ответа» и попадёт человеку.
    """

    refs = kb_article_ids or []
    t = store.get(ticket_id)
    if not t:
        return {"error": f"Тикет {ticket_id} не найден"}

    grounding = GUARDRAILS["grounding"]
    kinds = set(grounding["require_kb_citation_for"]) | set(grounding["non_closing_kinds"])
    if reply_kind not in kinds:
        return {"error": f"Недопустимый reply_kind. Доступно: {sorted(kinds)}"}

    if reply_kind in grounding["require_kb_citation_for"] and not refs:
        return {"error": "Ответ с решением обязан ссылаться на статью базы знаний. "
                         "Если подходящей статьи нет - это не решение: используй "
                         "reply_kind='status_update' или 'clarification', "
                         "но не подставляй произвольную статью."}

    # «Уведомление о статусе» законно только тогда, когда статус действительно
    # изменился: заявка эскалирована или по ней заведён баг-репорт.
    if reply_kind == "status_update":
        done = set(t.get("_agent", {})) & set(grounding["status_update_requires_action"])
        if not done:
            return {"error": "status_update разрешён после реального действия по заявке "
                             "(эскалация или баг-репорт). Сейчас по ней ничего не сделано."}

    threshold = GUARDRAILS["confidence"]["auto_close_min"]
    may_close = reply_kind not in grounding["non_closing_kinds"]
    blocked = close_ticket and may_close and confidence < threshold
    status = "Решена" if (close_ticket and may_close and not blocked) else "Ожидание ответа"

    store.patch(ticket_id, {"status": status}, actor="l1-agent",
                reason=f"Ответ пользователю ({reply_kind}), источники: {refs or '-'}")
    store.annotate(ticket_id, "reply", {
        "message": message, "kb_article_ids": refs, "reply_kind": reply_kind,
        "confidence": confidence, "auto_closed": status == "Решена",
    })
    audit("ticket.reply", ticket_id=ticket_id, kb_refs=refs, reply_kind=reply_kind,
          confidence=confidence, auto_closed=status == "Решена")

    out = {"ok": True, "ticket_id": ticket_id, "status": status,
           "reply_kind": reply_kind, "cited_articles": refs}
    if close_ticket and not may_close:
        out["note"] = (f"Ответ вида {reply_kind} заявку не закрывает - "
                       "решение по ней ещё не предоставлено.")
    if blocked:
        out["guardrail"] = (
            f"Автозакрытие заблокировано: confidence {confidence} < {threshold}. "
            "Ответ отправлен, тикет оставлен человеку на проверку."
        )

    return out


@mcp.tool()
def set_status(ticket_id: str, status: str, reason: str) -> dict:
    """Сменить статус заявки вручную."""

    if status not in STATUSES:
        return {"error": f"Недопустимый статус. Доступно: {STATUSES}"}
    if not store.get(ticket_id):
        return {"error": f"Тикет {ticket_id} не найден"}

    store.patch(ticket_id, {"status": status}, actor="l1-agent", reason=reason)

    return {"ok": True, "ticket_id": ticket_id, "status": status}


@mcp.tool()
def queue_stats() -> dict:
    """Сводка по очереди: сколько обработано агентом, распределение по маршрутам."""

    total = processed = corrected_cat = corrected_prio = 0
    by_path: dict[str, int] = {}
    by_status: dict[str, int] = {}

    for tid in store.all_ids():
        t = store.require(tid) # id взят из самого стора - заявка точно есть
        total += 1
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        triage = (t.get("_agent") or {}).get("triage")

        if triage:
            processed += 1
            by_path[triage["resolution_path"]] = by_path.get(triage["resolution_path"], 0) + 1
            orig = store.original(tid)
            corrected_cat += orig["category"] != t["category"]
            corrected_prio += orig["priority"] != t["priority"]

    return {
        "total": total,
        "processed_by_agent": processed,
        "by_resolution_path": by_path,
        "by_status": by_status,
        "corrected_category": corrected_cat,
        "corrected_priority": corrected_prio,
    }


if __name__ == "__main__":
    mcp.run()