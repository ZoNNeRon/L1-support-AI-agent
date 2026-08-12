"""
MCP-сервер «База знаний» - макет корпоративной GitHub Wiki.

Статьи лежат в data/kb/*.md с метаданными в YAML. Поиск - BM25 (common/kb_index.py).
Здесь же реализовано самообучение: агент дописывает новую статью, когда проблема
была решена вне базы знаний - но всегда в статусе draft, с обязательной
проверкой инженера.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from mcp.server.fastmcp import FastMCP

from common.kb_index import KB_DIR, KnowledgeBase, normalised_confidence
from common.store import ROOT, audit

mcp = FastMCP("knowledge-base")
TAXONOMY = yaml.safe_load((ROOT / "policies" / "taxonomy.yaml").read_text(encoding="utf-8"))
GUARDRAILS = yaml.safe_load((ROOT / "policies" / "guardrails.yaml").read_text(encoding="utf-8"))
CATEGORIES = [c["name"] for c in TAXONOMY["categories"]]


@mcp.tool()
def kb_search(query: str, top_k: int = 3, category: str = "") -> dict:
    """
    Найти статьи по тексту проблемы.

    Возвращает retrieval_confidence - нормализованную оценку уверенности поиска
    (учитывает и абсолютное значение, и отрыв топ-1 от топ-2). Если она низкая,
    значит готового решения в базе, скорее всего, нет: это сигнал в пользу
    эскалации или баг-репорта, а НЕ повод пересказать ближайшую попавшуюся статью.
    """

    kb = KnowledgeBase()
    hits = kb.search(query, top_k=top_k, category=category or None)
    conf = normalised_confidence([s for _, s in hits])

    audit("kb.search", query=query[:200],
          hits=[a.id for a, _ in hits], confidence=conf)

    return {
        "query": query,
        "retrieval_confidence": conf,
        "threshold_for_auto_close": GUARDRAILS["confidence"]["auto_close_min"],
        "results": [
            {
                "article_id": a.id,
                "title": a.title,
                "category": a.category,
                "status": a.status,
                "score": s,
                "excerpt": a.excerpt(300),
            }
            for a, s in hits
        ],
        "hint": (
            "Ничего релевантного не найдено - типовой инструкции нет, "
            "выбирай ESCALATE_L2 или BUG_REPORT"
            if not hits else
            "Обязательно открой статью через kb_get_article и убедись, что она "
            "действительно решает проблему пользователя, прежде чем цитировать"
        ),
    }


@mcp.tool()
def kb_get_article(article_id: str) -> dict:
    """Прочитать статью целиком, чтобы взять из неё точные шаги."""

    kb = KnowledgeBase()

    a = kb.get(article_id)
    if not a:
        return {"error": f"Статья {article_id} не найдена",
                "available": [x.id for x in kb.articles]}

    return {
        "article_id": a.id,
        "title": a.title,
        "category": a.category,
        "status": a.status,
        "keywords": a.keywords,
        "content": a.body.strip(),
    }


@mcp.tool()
def kb_list(category: str = "") -> dict:
    """Оглавление базы знаний."""

    kb = KnowledgeBase()
    arts = [a for a in kb.articles if not category or a.category == category]

    return {
        "count": len(arts),
        "articles": [
            {"article_id": a.id,
             "title": a.title,
             "category": a.category,
             "status": a.status
            } for a in arts
        ],
    }


@mcp.tool()
def kb_create_article(title: str, category: str, keywords: list[str], symptoms: str,
                      solution_steps: list[str], source_ticket_id: str,
                      source_reference: str = "") -> dict:
    """
    Создать новую статью по итогам решённой нетиповой проблемы (самообучение).

    Вызывается после того, как L2 или разработка подсказали решение: следующий
    похожий тикет должен закрываться уже по Сценарию А. Статья создаётся в статусе
    draft - публикует её человек, потому что непроверенная инструкция в базе знаний
    хуже, чем её отсутствие.

    source_reference: откуда взято решение (сообщение L2, комментарий в Issue).
    """

    if category not in CATEGORIES:
        return {"error": f"Недопустимая категория. Доступно: {CATEGORIES}"}
    if not solution_steps:
        return {"error": "Нужен хотя бы один шаг решения"}
    if not source_reference:
        return {"error": "Укажи источник решения - статья не может быть придумана агентом"}

    kb = KnowledgeBase()
    new_id = kb.next_id()
    status = GUARDRAILS["kb_enrichment"]["new_article_status"]
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(solution_steps, 1))

    # ВНИМАНИЕ: шаблон обязан начинаться с самого начала строки, без отступа.
    # Парсер фронтматтера в kb_index._parse() проверяет raw.startswith("---"),
    # поэтому любой отступ или пустая первая строка обнулят все метаданные статьи.
    doc = f"""---
id: {new_id}
title: {title}
category: {category}
keywords: [{', '.join(keywords)}]
resolution_path: KB_RESOLVE
status: {status}
created_by: l1-agent
created_at: {datetime.now(UTC).date().isoformat()}
source_ticket: {source_ticket_id}
source_reference: {source_reference}
---
## Симптомы
{symptoms}

## Решение
{steps}

## Происхождение
Статья создана ИИ-агентом первой линии по итогам тикета #{source_ticket_id}.
Источник решения: {source_reference}.
Требуется проверка инженера перед публикацией (текущий статус: {status}).
"""

    (KB_DIR / f"{new_id}.md").write_text(doc, encoding="utf-8")
    audit("kb.create_article", article_id=new_id, ticket_id=source_ticket_id, status=status)

    return {
        "ok": True,
        "article_id": new_id,
        "status": status,
        "path": f"data/kb/{new_id}.md",
        "note": "Статья доступна поиску сразу, но помечена как draft до проверки инженера.",
    }


@mcp.tool()
def kb_coverage_gaps() -> dict:
    """
    Показать, по каким темам база знаний пуста.

    Аналитический инструмент: агент не только закрывает тикеты, но и показывает
    руководству, где именно база знаний не покрывает поток обращений.
    """

    from collections import Counter

    from common.store import read_audit

    misses = Counter()
    for rec in read_audit():
        if rec.get("event") == "kb.search" and rec.get("confidence", 1) < \
                GUARDRAILS["confidence"]["auto_close_min"]:
            misses[rec.get("query", "")[:80]] += 1

    return {
        "low_confidence_queries": misses.most_common(15),
        "articles_total": len(KnowledgeBase().articles),
        "note": "Возможно, требуется новая статья (частые запросы с низкой уверенностью).",
    }


if __name__ == "__main__":
    mcp.run()