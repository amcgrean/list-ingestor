.PHONY: dev install docker-up docker-down docker-build reset-db help

## Run the Flask dev server (auto-reloads on file changes)
dev:
	python run.py

## Install Python dependencies into the active virtualenv
install:
	pip install -r requirements.txt

## Build and start the full stack with Docker Compose (Flask + PostgreSQL)
docker-up:
	docker compose up --build

## Stop Docker Compose services
docker-down:
	docker compose down

## Rebuild Docker image without cache (useful after changing requirements.txt)
docker-build:
	docker compose build --no-cache

## Delete the local SQLite database (forces a fresh schema on next run)
reset-db:
	rm -f data/app.db
	@echo "Database cleared. It will be re-created on next run."

## Print help
help:
	@grep -E '^##' Makefile | sed 's/## //'
