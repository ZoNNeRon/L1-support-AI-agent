"""
Eval слоя поиска - базовая линия без LLM.

Зачем отдельно от eval агента: если поиск по базе знаний промахивается, никакая
модель сверху это не спасёт - она либо пересказывает не ту статью, либо
галлюцинирует шаги. Поэтому retrieval меряется изолированно и раньше всего:
это базовая линия, поверх которой потом считаются метрики агента целиком
(уровень 2 - accuracy категории, точность маршрута, grounding-rate).

Метрики:
  top-1 accuracy / top-3 accuracy - попадание эталонной статьи в топ выдачи;
  routing accuracy - правильно ли пороги уверенности разделяют
                     «есть инструкция» / «нужна эскалация или баг-репорт»;
  false-resolve    - самая дорогая ошибка: система уверенно закрывает тикет
                     статьёй, которая проблему не решает.

Запуск: python eval/eval_retrieval.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml

from common.kb_index import KnowledgeBase, normalised_confidence
from common.store import ROOT

REFERENCE = json.loads((ROOT / "eval" / "reference_set.json").read_text(encoding="utf-8"))
GUARDRAILS = yaml.safe_load((ROOT / "policies" / "guardrails.yaml").read_text(encoding="utf-8"))
AUTO_CLOSE = GUARDRAILS["confidence"]["auto_close_min"]
RESULTS = ROOT / "eval" / "results_retrieval.json"


def main() -> int:
    kb = KnowledgeBase()
    labels = REFERENCE["labels"]
    rows, top1acc, top3acc, n_article = [], 0, 0, 0
    routing_ok = false_resolve = missed_resolve = 0

    for lb in labels:
        # include_drafts=False - базовая линия меряется по проверенной человеком
        # базе. Черновики, написанные агентом в контуре самообучения, доступны
        # ему в работе, но в эталон не входят: иначе базовая линия менялась бы
        # после каждого прогона и перестала быть сравнимой.
        hits = kb.search(lb["description"], top_k=3, include_drafts=False)
        ids = [a.id for a, _ in hits]
        conf = normalised_confidence([s for _, s in hits])

        if lb["article"]:
            n_article += 1
            top1acc += ids[:1] == [lb["article"]]
            top3acc += lb["article"] in ids

        # Решение слоя поиска: хватает ли уверенности, чтобы закрыть по базе знаний
        predicted = "KB_RESOLVE" if conf >= AUTO_CLOSE else "NOT_KB"
        expected = "KB_RESOLVE" if lb["path"] == "KB_RESOLVE" else "NOT_KB"
        ok = predicted == expected
        routing_ok += ok
        if predicted == "KB_RESOLVE" and expected == "NOT_KB":
            false_resolve += 1 # закрыли бы то, что закрывать нельзя
        if predicted == "NOT_KB" and expected == "KB_RESOLVE":
            missed_resolve += 1 # отдали бы человеку то, что решается само

        rows.append({
            "description": lb["description"][:48],
            "expected_article": lb["article"] or "-",
            "top1": ids[0] if ids else "-",
            "conf": conf,
            "expected_path": lb["path"],
            "verdict": "OK" if ok else ("FALSE-RESOLVE" if predicted == "KB_RESOLVE" else "MISS"),
        })

    n = len(labels)
    drafts = [a for a in kb.articles if a.status != "published"]
    print(f"\nЭталонных описаний: {n} (покрывают 100 тикетов очереди)")
    print(f"Статей в базе знаний: {len(kb.articles) - len(drafts)} проверенных"
          + (f", черновиков агента: {len(drafts)} (в базовую линию не входят)" if drafts else ""))
    print(f"Порог автозакрытия: {AUTO_CLOSE}\n")
    print(f"{'описание':<50}{'эталон':<10}{'top1':<10}{'conf':<7}{'вердикт'}")
    print("-" * 100)
    for r in rows:
        print(f"{r['description']:<50}{r['expected_article']:<10}{r['top1']:<10}"
              f"{r['conf']:<7}{r['verdict']}")

    print("\nМЕТРИКИ СЛОЯ ПОИСКА")
    print(f"top-1 accuracy   {top1acc}/{n_article} = {top1acc / n_article:.1%}")
    print(f"top-3 accuracy   {top3acc}/{n_article} = {top3acc / n_article:.1%}")
    print(f"routing accuracy {routing_ok}/{n} = {routing_ok / n:.1%}")
    print(f"false-resolve    {false_resolve}  (закрыли бы тикет чужой статьёй)")
    print(f"missed-resolve   {missed_resolve}  (зря отдали бы человеку)")
    print("\nfalse-resolve - критичная ошибка: пользователь получает нерелевантную")
    print("инструкцию и теряет время. missed-resolve дешевле: тикет просто уходит")
    print("человеку. Пороги в policies/guardrails.yaml подобраны с этим перекосом.\n")

    RESULTS.write_text(
        json.dumps({"top-1 accuracy": top1acc / n_article, "top-3 accuracy": top3acc / n_article,
                    "routing_accuracy": routing_ok / n, "false_resolve": false_resolve,
                    "missed_resolve": missed_resolve, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())