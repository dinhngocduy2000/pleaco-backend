"""Start the API with a safely encoded restricted-role database URL."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL


def main() -> None:
    local = sys.argv[1:] == ["--local"]
    if local:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    environment = os.environ.copy()
    password = environment.pop("APP_DB_PASSWORD")
    database = environment.pop("APP_DB_NAME", None) or environment.get("POSTGRES_DB")
    if not database:
        raise RuntimeError("APP_DB_NAME or POSTGRES_DB is required")
    environment.pop("POSTGRES_USER", None)
    environment.pop("POSTGRES_PASSWORD", None)
    environment.pop("POSTGRES_DB", None)
    environment["DATABASE_URL"] = URL.create(
        "postgresql+asyncpg",
        username="pleaco_app",
        password=password,
        host="127.0.0.1" if local else "db",
        port=5432,
        database=database,
    ).render_as_string(hide_password=False)
    port = environment.pop("DEV_PORT", "8000") if local else "8000"
    command = [
        sys.executable, "-m", "uvicorn", "app.cmd.main:app", "--host", "0.0.0.0",
        "--port", port, "--reload",
    ]
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
