PYTHON ?= python3.11
VENV ?= .venv
VENV_PYTHON = $(VENV)/bin/python

# Runtime configuration is loaded by the application and Docker Compose.
.DEFAULT_GOAL := help

.PHONY: help venv debug dev-test install install-dev dev docker-up docker-down migrate migration migration-downgrade migration-status migration-history test test-unit test-integration test-cov test-watch lint format clean

help:
	@echo "Available commands:"
	@echo "  make venv              - Create the local Python virtual environment"
	@echo "  make install           - Install dependencies into the virtual environment"
	@echo "  make install-dev       - Install dev dependencies"
	@echo "  make dev               - Run development server"
	@echo "  make dev-test          - Run server with .env.test"
	@echo "  make docker-up         - Start Docker containers"
	@echo "  make docker-down       - Stop Docker containers"
	@echo "  make migrate               - Run database migrations"
	@echo "  make migration             - Create new migration"
	@echo "  make migration-downgrade   - Downgrade database by 1 migration"
	@echo "  make migration-status      - Check migration status"
	@echo "  make migration-history     - Show detailed migration history"
	@echo "  make test              - Run all tests"
	@echo "  make test-unit         - Run unit tests only"
	@echo "  make test-integration  - Run integration tests only"
	@echo "  make test-cov          - Run tests with coverage"
	@echo "  make test-watch        - Run tests in watch mode"
	@echo "  make lint              - Run linting"
	@echo "  make format            - Format code"
	@echo "  make clean             - Clean up cache files"
	@echo "  make build-image       - Build Docker image"
	@echo "  make tag-image         - Tag Docker image"
	@echo "  make push-image        - Push Docker image to ECR"

venv: $(VENV)/bin/python

$(VENV)/bin/python:
	$(PYTHON) -m venv "$(VENV)"

install: venv
	"$(VENV_PYTHON)" -m pip install -r requirements.txt

install-dev: venv
	"$(VENV_PYTHON)" -m pip install -r requirements.txt -r requirements-dev.txt

dev:
	"$(VENV_PYTHON)" -m uvicorn app.cmd.main:app --reload --host 0.0.0.0 --port 8000

debug:
	"$(VENV_PYTHON)" -m debugpy --listen 0.0.0.0:5678 --wait-for-client -m uvicorn app.cmd.main:app --reload --host 0.0.0.0 --port 8000

dev-test:
	ENV_FILE=.env.test "$(VENV_PYTHON)" -m uvicorn app.cmd.main:app --reload --host 0.0.0.0 --port 8000

docker-up:
	docker-compose up -d
	@echo "Waiting for services to start..."
	@sleep 5
	docker-compose exec api alembic upgrade head

start-db: 
	docker-compose up -d db

docker-down:
	docker-compose down

migrate:
	alembic upgrade head

migration-downgrade:
	alembic downgrade -1

migration:
	@read -p "Enter migration message: " message; \
	alembic revision --autogenerate -m "$$message"

migration-status:
	@echo "Current database revision:"
	@alembic current
	@echo "\nLatest available revision:"
	@alembic heads
	@echo "\nMigration history:"
	@alembic history

migration-history:
	alembic history --verbose

test:
	"$(VENV_PYTHON)" -m pytest -v

test-unit:
	"$(VENV_PYTHON)" -m pytest tests/unit -v

test-integration:
	"$(VENV_PYTHON)" -m pytest tests/integration -v

test-cov:
	"$(VENV_PYTHON)" -m pytest --cov=app --cov-report=html --cov-report=term

test-watch:
	pytest-watch -- -v

lint:
	flake8 app/ tests/

format:
	black app/ tests/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".coverage" -exec rm -rf {} + 2>/dev/null || true

build-image:
	docker build -t pleaco-backend:latest .

tag-image: 
	docker tag pleaco-backend:latest $(AWS_ECR_URL):latest

push-image: 
	docker push $(AWS_ECR_URL):latest
