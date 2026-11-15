"""Run Alembic with the provisioning role's safely encoded database URL."""

import os

from sqlalchemy.engine import URL


def main() -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = URL.create(
        "postgresql+asyncpg",
        username=environment["POSTGRES_USER"],
        password=environment["POSTGRES_PASSWORD"],
        host="db",
        port=5432,
        database=environment["POSTGRES_DB"],
    ).render_as_string(hide_password=False)
    os.execvpe("alembic", ["alembic", "upgrade", "head"], environment)


if __name__ == "__main__":
    main()
