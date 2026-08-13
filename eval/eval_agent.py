"""
Eval агента - уровень 2, поверх заведомо рабочего слоя поиска.

Считается не по повторному прогону модели, а по журналу решений
data/shadow/audit.jsonl и накопленному overlay. Причина: прогон агента
недетерминирован и стоит денег, а журнал уже содержит всё, что агент сделал,
и воспроизводим бесплатно. Один прогон - сколько угодно пересчётов метрик.

Метрики:
  покрытие            сколько заявок обработано и как распределились маршруты;
  category accuracy   совпала ли восстановленная категория с эталонной;
  route accuracy      верно ли выбран сценарий А / Б / В;
  priority MAE        на сколько ступеней шкалы приоритет разошёлся с эталоном;
  grounding rate      доля ответов со ссылкой на реально существующую статью;
  исправление         сколько значений в очереди было неверно и что с ними стало:
                      исправлено / пропущено / испорчено верных;
  калибровка          как часто агент поднимал уверенность выше той, что дал поиск;
  guardrails          нарушений быть не должно - это проверка, а не измерение.

Запуск: python eval/eval_agent.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml

from common.kb_index import KnowledgeBase
from common.store import ROOT

AUDIT = ROOT / "data" / "shadow" / "audit.jsonl"
OVERLAY = ROOT / "data" / "shadow" / "overlay.json"
SNAPSHOT = ROOT / "data" / "cache" / "tickets_snapshot.json"
REFERENCE = ROOT / "eval" / "reference_set.json"
MATRIX = ROOT / "policies" / "priority_matrix.yaml"
GUARDRAILS = ROOT / "policies" / "guardrails.yaml"
RESULTS = ROOT / "eval" / "results_agent.json"

# Шкала приоритетов в порядке возрастания - нужна, чтобы считать MAE в ступенях.
SCALE = ["Низкий", "Средний", "Высокий", "Критический"]


def expected_priority(impact: str, urgency: str, text: str, matrix: dict) -> str:
    """
    Эталонный приоритет по разметке.

    Матрица перечитывается здесь заново, а не импортируется из MCP-сервера:
    eval не должен зависеть от кода, который измеряет, иначе одинаковая ошибка
    в обеих половинах взаимно скроется.
    """

    score = matrix["impact"][impact] * matrix["urgency"][urgency]
    priority = matrix["matrix"][score]
    low = text.lower()

    for override in matrix.get("overrides", []):
        if any(signal in low for signal in override["if_signals"]):
            return override["priority"]
        
    return priority


def normalise(text: str) -> str:
    """
    Ключ для сопоставления запроса из журнала с описанием заявки.

    Агент ищет по тексту описания, но обычно без финальной точки и с иным
    регистром. Сравнение «как есть» тихо не находило бы совпадений, а метрика
    калибровки показывала бы ноль вместо реальных значений.
    """

    return " ".join(text.lower().replace("ё", "е").split()).strip(" .!?;:")


def load_audit() -> list[dict]:

    if not AUDIT.exists():
        return []
    
    return [json.loads(line) for line in AUDIT.read_text(encoding="utf-8").splitlines() 
            if line.strip()]


def main() -> int:

    if not OVERLAY.exists():
        print("\nАгент ещё не обрабатывал очередь: data/shadow/overlay.json отсутствует.")
        print("Нужно запустить claude из корня проекта и попросить разобрать заявки.\n")
        return 1

    overlay = json.loads(OVERLAY.read_text(encoding="utf-8"))
    snapshot = {t["id"]: t for t in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}
    reference = {lb["description"]: lb for lb in
                 json.loads(REFERENCE.read_text(encoding="utf-8"))["labels"]}
    matrix = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    guardrails = yaml.safe_load(GUARDRAILS.read_text(encoding="utf-8"))
    auto_close_min = guardrails["confidence"]["auto_close_min"]
    known_articles = {a.id for a in KnowledgeBase().articles}
    audit = load_audit()

    # Уверенность поиска по тексту запроса - понадобится для калибровки.
    search_conf = {normalise(r["query"]): r["confidence"]
                   for r in audit if r["event"] == "kb.search"}

    rows: list[dict] = []
    by_path: dict[str, int] = {}
    cat_ok = cat_total = route_ok = route_total = 0
    prio_diffs: list[int] = []
    # Исправление исходной классификации разбирается на четыре исхода: сколько
    # значений в очереди было неверно, сколько из них агент починил, сколько
    # пропустил и сколько верных испортил. Без знаменателя «было неверно» голое
    # число исправлений ничего не сообщает: ноль исправлений одинаково выглядит
    # и когда чинить было нечего, и когда агент проспал весь беспорядок.
    fix = {f"{field}_{outcome}": 0
           for field in ("cat", "prio")
           for outcome in ("need", "fixed", "missed", "broken")}
    grounded = replies = auto_closed = 0
    raised, raised_and_closed = 0, 0
    violations: list[str] = []

    for tid in sorted(overlay, key=int):
        state, original = overlay[tid], snapshot.get(tid)

        if original is None:
            continue

        agent = state.get("_agent", {})
        triage = agent.get("triage")
        if not triage:
            continue # заявку тронули, но не классифицировали - в метрики не идёт

        label = reference.get(original["description"])
        path = triage["resolution_path"]
        by_path[path] = by_path.get(path, 0) + 1

        category = state.get("category", original["category"])
        priority = state.get("priority", original["priority"])

        row = {"ticket_id": tid, "category": category, "priority": priority,
               "path": path, "confidence": triage.get("confidence")}

        if label:
            cat_total += 1
            cat_ok += label["category"] == category
            route_total += 1
            route_ok += label["path"] == path
            want = expected_priority(label["impact"], label["urgency"],
                                     f"{original['title']} {original['description']}", matrix)
            diff = abs(SCALE.index(priority) - SCALE.index(want)) if priority in SCALE else 0
            prio_diffs.append(diff)
            row |= {"expected_category": label["category"], "expected_path": label["path"],
                    "expected_priority": want, "priority_diff": diff}

            for field, was, now, want_value in (
                ("cat", original["category"], category, label["category"]),
                ("prio", original["priority"], priority, want),
            ):
                if was != want_value:                     # в очереди стояло неверное значение
                    fix[f"{field}_need"] += 1
                    fix[f"{field}_{'fixed' if now == want_value else 'missed'}"] += 1
                elif now != want_value:                   # было верно, а стало нет
                    fix[f"{field}_broken"] += 1

        reply = agent.get("reply")
        if reply:
            replies += 1
            refs = reply.get("kb_article_ids", [])

            if refs and all(r in known_articles for r in refs):
                grounded += 1
            elif not refs:
                violations.append(f"тикет {tid}: ответ без ссылки на статью")
            else:
                unknown = [r for r in refs if r not in known_articles]
                violations.append(f"тикет {tid}: ссылка на несуществующие статьи {unknown}")

            confidence = reply.get("confidence", 0.0)
            if reply.get("auto_closed"):
                auto_closed += 1
                if confidence < auto_close_min:
                    violations.append(
                        f"тикет {tid}: автозакрытие при уверенности {confidence} "
                        f"< порога {auto_close_min}"
                    )
            # Калибровка: агент вправе поднять уверенность после чтения статьи -
            # он видел текст, которого у BM25 не было. Но это должно быть видно.
            found = search_conf.get(normalise(original["description"]))
            if found is not None and confidence > found:
                raised += 1
                if reply.get("auto_closed"):
                    raised_and_closed += 1
                row["search_confidence"] = found
                row["reply_confidence"] = confidence

        rows.append(row)

    if not rows:
        print("\nВ overlay нет классифицированных заявок - считать нечего.\n")
        return 1

    n = len(rows)
    print(f"\nОбработано заявок агентом: {n} из {len(snapshot)} в очереди")
    print(f"Записей в журнале решений: {len(audit)}\n")

    print(f"{'тикет':<8}{'категория':<24}{'приоритет':<14}{'маршрут':<14}{'conf':<7}{'сверка'}")
    print("-" * 100)
    for r in rows:
        marks = []
        if "expected_category" in r and r["expected_category"] != r["category"]:
            marks.append(f"категория≠{r['expected_category']}")
        if "expected_path" in r and r["expected_path"] != r["path"]:
            marks.append(f"маршрут≠{r['expected_path']}")
        if r.get("priority_diff"):
            marks.append(f"приоритет≠{r['expected_priority']}")
        print(f"{r['ticket_id']:<8}{r['category']:<24}{r['priority']:<14}{r['path']:<14}"
              f"{r['confidence']!s:<7}{'OK' if not marks else ', '.join(marks)}")

    print("\nРАСПРЕДЕЛЕНИЕ ПО МАРШРУТАМ")
    for path in ("KB_RESOLVE", "ESCALATE_L2", "BUG_REPORT", "NEED_INFO"):
        count = by_path.get(path, 0)
        print(f"  {path:<14}{count:>3}")
    missing = [p for p in ("KB_RESOLVE", "ESCALATE_L2", "BUG_REPORT") if not by_path.get(p)]
    if missing:
        print(f"  не проверены сценарии: {', '.join(missing)} - метрики по ним отсутствуют")

    print("\nМЕТРИКИ АГЕНТА")
    metrics: dict[str, float | int] = {}
    if cat_total:
        metrics["category_accuracy"] = cat_ok / cat_total
        print(f"category accuracy  {cat_ok}/{cat_total} = {cat_ok / cat_total:.1%}")
    if route_total:
        metrics["route_accuracy"] = route_ok / route_total
        print(f"route accuracy     {route_ok}/{route_total} = {route_ok / route_total:.1%}")
    if prio_diffs:
        mae = sum(prio_diffs) / len(prio_diffs)
        exact = sum(1 for d in prio_diffs if d == 0)
        metrics |= {"priority_mae": mae, "priority_exact": exact / len(prio_diffs)}
        print(f"priority MAE       {mae:.2f} ступени шкалы "
              f"(точное совпадение {exact}/{len(prio_diffs)})")
    if replies:
        metrics["grounding_rate"] = grounded / replies
        print(f"grounding rate     {grounded}/{replies} = {grounded / replies:.1%}")
        metrics["auto_close_rate"] = auto_closed / replies
        print(f"доля автозакрытий  {auto_closed}/{replies} = {auto_closed / replies:.1%}"
              f"  (остальные ушли человеку)")
    metrics |= {**fix, "processed": n}

    print("\nИСПРАВЛЕНИЕ ИСХОДНОЙ КЛАССИФИКАЦИИ")
    print("сколько значений в очереди было проставлено неверно и что с ними стало")
    print(f"  {'':<12}{'было неверно':<15}{'исправлено':<13}{'пропущено':<12}{'испорчено'}")
    nothing_to_fix = []
    for field, title in (("cat", "категория"), ("prio", "приоритет")):
        need = fix[f"{field}_need"]
        share = f"{need}/{cat_total}" if cat_total else "-"
        fixed = f"{fix[f'{field}_fixed']}/{need}" if need else "-"
        print(f"  {title:<12}{share:<15}{fixed:<13}"
              f"{fix[f'{field}_missed']:<12}{fix[f'{field}_broken']}")
        if not need:
            nothing_to_fix.append(title)
    if nothing_to_fix:
        print(f"  {', '.join(nothing_to_fix)}: неверных значений в выборке не было, "
              "исправлять было нечего")
    print("  «испорчено» - агент изменил значение, которое и так было верным.")
    print("  Это самая дорогая ошибка в таблице, норма - ноль.")

    print("\nКАЛИБРОВКА УВЕРЕННОСТИ")
    metrics["confidence_raised"] = raised
    if raised:
        print(f"агент поднимал уверенность выше поисковой в {raised} случаях, "
              f"из них закрыл {raised_and_closed}")
        print("это разрешено: после чтения статьи у модели больше данных, чем у BM25.")
        print("сверить обоснованность можно по search_confidence/reply_confidence в results_agent.json")
    else:
        print("агент ни разу не поднимал уверенность выше поисковой")

    print("\nGUARDRAILS")
    if violations:
        print(f"НАРУШЕНИЙ: {len(violations)}")
        for v in violations:
            print(f"  - {v}")
    else:
        print("нарушений нет: все ответы обоснованы статьями, "
              "автозакрытий ниже порога не было")
    metrics["guardrail_violations"] = len(violations)

    RESULTS.write_text(
        json.dumps({**metrics, "by_resolution_path": by_path,
                    "violations": violations, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nПодробности: {RESULTS.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())