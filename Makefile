.PHONY: install dev worker run test lint format up down demo

VENV ?= .venv
PY := $(VENV)/bin/python

install:            ## Create a virtualenv and install dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install -r requirements-dev.txt

dev:                ## Run the API with tasks executed inline (no Redis/worker needed)
	CELERY_TASK_ALWAYS_EAGER=true $(VENV)/bin/uvicorn app.main:app --reload

run:                ## Run the API (needs Redis + a worker)
	$(VENV)/bin/uvicorn app.main:app --reload

worker:             ## Run a Celery worker
	$(VENV)/bin/celery -A app.worker.celery_app worker --loglevel=info --concurrency=4

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check . && $(VENV)/bin/ruff format --check .

format:
	$(VENV)/bin/ruff check --fix . && $(VENV)/bin/ruff format .

up:                 ## Start Postgres, Redis, API and worker with Docker
	docker compose up --build -d

down:
	docker compose down

demo:               ## Submit a sample bulk job against a running API
	$(PY) scripts/demo.py
