# AP Invoice Tracking Agent

Portfolio project. Mock of a purchasing system ("Octopos"). ALL data is fake. Never use real company data.

## Rules

- Python 3.12, managed by uv. Type hints everywhere, Pydantic v2 models.
- Use the LLM ONLY for invoice extraction and anomaly explanation. Everything else is plain code/SQL.
- Every DB write must be idempotent (upsert or check-before-insert).
- Never initiate a payment without a human approval step.
- Secrets come from src/ap_agent/config.py only. Never hardcode keys. Never print secret values.
- Never read, print or modify .env. Use .env.example for key names.
- Write/adjust pytest tests for any matching or duplicate logic.
- Run `uv run ruff check . && uv run pytest` before saying a task is done.
- No Docker needed locally; containers are built in CI later.

## Commands

- uv run pytest
- uv run ruff check .
- uv run python -m ap_agent.poller
- uv run uvicorn ap_agent.api.main:app --reload
- uv run streamlit run dashboard/app.py
