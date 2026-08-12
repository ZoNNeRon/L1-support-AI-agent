"""
MCP-сервер «GitHub Issues» - Сценарий В: дефект ПО уходит в разработку.

Макет репозитория разработчиков.

Задача инструмента - перенести технический контекст из заявки, приложить логи и
связать Issue с исходным тикетом. Перед созданием выполняется поиск дубликатов.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from mcp.server.fastmcp import FastMCP

from common.kb_index import tokenise
from common.store import ROOT, TicketStore, audit, now

mcp = FastMCP("github-issues")
store = TicketStore()
ISSUES = ROOT / "data" / "github" / "issues.json"
ROUTING = yaml.safe_load((ROOT / "policies" / "routing.yaml").read_text(encoding="utf-8"))


def _load() -> list[dict]:
    """Загрузка файла data/github/issues.json."""
    return json.loads(ISSUES.read_text(encoding="utf-8")) if ISSUES.exists() else []


def _save(items: list[dict]) -> None:
    """Сохранение в файл data/github/issues.json."""

    ISSUES.parent.mkdir(parents=True, exist_ok=True)
    ISSUES.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _similarity(a: str, b: str) -> float:
    """Проверка на наличие совпадений в двух текстовых строках."""

    ta, tb = set(tokenise(a)), set(tokenise(b))
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


@mcp.tool()
def find_duplicate_issue(summary: str, threshold: float = 0.45) -> dict:
    """Проверить, не заведён ли уже такой баг. Вызывать ДО create_bug_report."""

    cands = []
    for issue in _load():
        similarity = _similarity(summary, issue["title"] + " " + issue["body"][:400])
        if similarity >= threshold: # threshold - настраиваемый порог
            cands.append({"number": issue["number"], "title": issue["title"],
                          "state": issue["state"], "similarity": round(similarity, 2),
                          "linked_tickets": issue["linked_tickets"]})

    cands.sort(key=lambda x: -x["similarity"])

    return {
        "duplicates_found": len(cands), "candidates": cands[:3],
        "recommendation": (
            "Дубликат найден - не создавай новый Issue, вызови link_ticket_to_issue"
            if cands else "Дубликатов нет, можно создавать Issue"
        ),
    }


@mcp.tool()
def create_bug_report(ticket_id: str, title: str, steps_to_reproduce: list[str],
                      expected: str, actual: str, technical_context: str,
                      logs: str = "", severity: str = "Средний") -> dict:
    """Завести Bug Report в репозитории разработчиков и связать его с тикетом."""

    t = store.get(ticket_id)

    if not t:
        return {"error": f"Тикет {ticket_id} не найден"}

    duplicate = find_duplicate_issue(f"{title} {actual}")
    if duplicate["duplicates_found"]:
        return {"blocked": True, "reason": "Похоже, такой Issue уже существует",
                **duplicate}

    cfg = ROUTING["dev_teams"].get(t["category"], ROUTING["dev_teams"]["Ошибки в работе ПО"])
    items = _load()
    number = max((i["number"] for i in items), default=100) + 1
    reproduce = "\n".join(f"{i}. {s}" for i, s in enumerate(steps_to_reproduce, 1))

    # Шаблон без отступа: в markdown любые 4 пробела в начале строки превращают
    # текст в блок кода, и весь Issue отрендерился бы одной серой простынёй.
    body = f"""## Описание
{actual}

## Шаги воспроизведения
{reproduce}

## Ожидаемое поведение
{expected}

## Фактическое поведение
{actual}

## Технический контекст
{technical_context}

## Логи
```
{logs or 'Логи пользователем не приложены - запрошены через тикет.'}
```

## Источник
Заведено ИИ-агентом L1 из тикета #{ticket_id} (автор обращения: {t['user']}).
Исходная формулировка пользователя: «{t['description']}».
"""

    issue = {
        "number": number, "repo": cfg["repo"], "title": title, "body": body,
        "labels": cfg["labels"] + [f"severity:{severity}"],
        "assignee_hint": cfg["assignee_hint"], "state": "open",
        "created_at": now(), "created_by": "l1-agent",
        "linked_tickets": [str(ticket_id)],
    }
    items.append(issue)
    _save(items)

    url = f"https://github.com/{cfg['repo']}/issues/{number}"
    store.annotate(ticket_id, "bug_report", {"issue": number, "url": url})
    store.patch(ticket_id, {"status": "В работе"}, actor="l1-agent",
                reason=f"Заведён баг-репорт {url}")
    audit("github.create_issue", ticket_id=ticket_id, issue=number, repo=cfg["repo"])

    return {"ok": True, "issue_number": number, "url": url,
            "labels": issue["labels"], "assignee_hint": cfg["assignee_hint"]}


@mcp.tool()
def link_ticket_to_issue(ticket_id: str, issue_number: int) -> dict:
    """Привязать тикет к уже существующему Issue (случай дубликата)."""

    items = _load()
    for issue in items:
        if issue["number"] == issue_number:

            if str(ticket_id) not in issue["linked_tickets"]:
                issue["linked_tickets"].append(str(ticket_id))

            _save(items)
            url = f"https://github.com/{issue['repo']}/issues/{issue_number}"

            store.annotate(ticket_id, "bug_report", {"issue": issue_number, "url": url,
                                                     "duplicate_of_existing": True})
            audit("github.link_ticket", ticket_id=ticket_id, issue=issue_number)

            return {"ok": True, "issue_number": issue_number, "url": url,
                    "linked_tickets": issue["linked_tickets"]}

    return {"error": f"Issue #{issue_number} не найден"}


@mcp.tool()
def list_issues(state: str = "open") -> dict:
    """Список заведённых Issue."""

    items = [i for i in _load() if not state or i["state"] == state]

    return {"count": len(items),
            "issues": [
                {"number": i["number"],
                 "title": i["title"],
                 "state": i["state"],
                 "labels": i["labels"],
                 "linked_tickets": i["linked_tickets"]
                } for i in items]}


@mcp.tool()
def resolve_issue(issue_number: int, resolution_note: str) -> dict:
    """
    Закрыть Issue с описанием решения от разработчиков.

    Используется в демо-сценарии self-learning: решение из Issue становится
    основой новой статьи базы знаний.
    """

    items = _load()

    for issue in items:
        if issue["number"] == issue_number:

            issue.update({"state": "closed", "resolution": resolution_note,
                        "closed_at": now()})

            _save(items)
            audit("github.resolve_issue", issue=issue_number)

            return {"ok": True,
                    "issue_number": issue_number,
                    "linked_tickets": issue["linked_tickets"],
                    "next_step": "Создай статью в базе знаний через kb_create_article"}

    return {"error": f"Issue #{issue_number} не найден"}


if __name__ == "__main__":
    mcp.run()