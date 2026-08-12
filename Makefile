.PHONY: install snapshot eval reset demo

install:
	uv sync --extra dev

snapshot:
	uv run python scripts/fetch_snapshot.py

eval:
	uv run python eval/eval_retrieval.py

reset:
	uv run python -c "from common.store import TicketStore; TicketStore().reset()"
	rm -f data/shadow/audit.jsonl data/github/issues.json data/telegram/messages.json

demo:
	uv run python agent/run_queue.py --limit 10