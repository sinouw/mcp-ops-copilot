.PHONY: install dev api web seed test lint typecheck eval eval-hijacked check up down

install:
	uv sync
	npm --prefix web ci

api:            ## API on :8000 with an embedded Postgres (no Docker needed)
	uv run opscopilot serve --embedded-db

web:            ## console on :5173 (proxies /api to :8000)
	npm --prefix web run dev

seed:
	uv run opscopilot seed --embedded-db

test:
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy
	npm --prefix web run typecheck

eval:           ## offline scenario suite (scripted model); use PROVIDER=groq MODEL=... for a real LLM
	uv run opscopilot eval --embedded-db $(if $(PROVIDER),--provider $(PROVIDER)) $(if $(MODEL),--model $(MODEL))

eval-hijacked:  ## same suite against a model that obeys every injected instruction
	uv run opscopilot eval --embedded-db --provider scripted-hijacked

check: lint typecheck test eval eval-hijacked   ## everything CI runs
	uv run python evals/assert_thresholds.py evals/results
	npm --prefix web run build

up:
	docker compose up --build

down:
	docker compose down -v
