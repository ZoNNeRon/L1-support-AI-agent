.PHONY: install snapshot eval eval-agent reset demo-seed demo-clean

install:
	uv sync --extra dev

snapshot:
	uv run python scripts/fetch_snapshot.py

# Уровень 1 - слой поиска, без LLM. Воспроизводится всегда.
eval:
	uv run python eval/eval_retrieval.py

# Уровень 2 - агент целиком. Считается по журналу решений,
# поэтому требует хотя бы одного прогона агента в Claude Code.
eval-agent:
	uv run python eval/eval_agent.py

reset:
	uv run python -c "from common.store import TicketStore; TicketStore().reset()"
	rm -f data/shadow/audit.jsonl data/github/issues.json \
	      data/telegram/messages.json data/telegram/inbox.json

# Четыре заявки, покрывающие все сценарии - для записи демонстрации.
demo-seed:
	uv run python scripts/seed_demo_tickets.py

# Чистый старт перед записью: состояние агента, артефакты каналов, черновики
# статей из прошлых прогонов и свежие демо-заявки в очереди. Удаляются только
# черновики агента, не попавшие в git, - содержимое репозитория не трогается.
demo-clean: reset
	uv run python scripts/seed_demo_tickets.py --clean-drafts
	uv run python scripts/seed_demo_tickets.py --remove
	uv run python scripts/seed_demo_tickets.py