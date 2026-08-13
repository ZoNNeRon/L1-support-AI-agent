.PHONY: install snapshot eval eval-agent reset

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
	rm -f data/shadow/audit.jsonl data/github/issues.json data/telegram/messages.json