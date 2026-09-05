# pleaco-backend

## Local Python setup

Use Python 3.11, matching the Docker image. Python's `venv` isolates dependencies;
Uvicorn is the ASGI server that runs FastAPI and is already in `requirements.txt`.

From the repository root on macOS or Linux:

```bash
make install-dev
make dev
```

`make install-dev` creates `.venv` and installs the runtime and development
requirements. Use `make install` for runtime dependencies only, or `make venv`
to create an empty environment. To select a Python 3.11 executable explicitly:

```bash
make install-dev PYTHON=/path/to/python3.11
```

The development server runs `app.cmd.main:app` on port 8000 with automatic reload.
Configure the application's required runtime settings locally and start its
dependencies before running it:

```bash
docker compose up -d db redis rabbitmq mosquitto
```

For a host-based API, configure service connections to use the published localhost
ports instead of Docker service names. Avoid running the Compose `api` service
at the same time as `make dev`, since both use port 8000.

Make does not load `.env`; the application and Docker Compose handle runtime
configuration. Supply Make-only variables explicitly, for example
`make tag-image AWS_ECR_URL=<registry/repository>`.

## Interpreter and commands

Select `.venv/bin/python` with **Python: Select Interpreter** in VS Code or Cursor.
The install, development server, debugger, and pytest Make targets use `.venv`
without requiring activation. For direct terminal commands:

```bash
source .venv/bin/activate
python -m uvicorn app.cmd.main:app --reload --host 0.0.0.0 --port 8000
# When finished:
deactivate
```

Verify the installation without starting the application:

```bash
.venv/bin/python -m pip check
.venv/bin/python -m uvicorn --version
```

Run tests with `make test` or `make test-unit` once their configuration and service
requirements are available. `.venv` is already excluded from Git and Docker builds.
