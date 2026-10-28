"""Run queue tests using synthetic settings, without environment/secret sources.

Usage: .venv/bin/python scripts/test_rabbitmq.py [pytest arguments]
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_settings import BaseSettings
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource, InitSettingsSource

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def isolated_sources(cls, settings_cls, init_settings, **_sources):
    return (
        init_settings,
        InitSettingsSource(settings_cls, init_kwargs={
            "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
            "SECRET_KEY": "queue-tests-only",
            "ALGORITHM": "HS256",
            "ACCESS_TOKEN_EXPIRE_SECONDS": 60,
            "REDIS_PASSWORD": "queue-tests-only",
        }),
    )


if __name__ == "__main__":
    arguments = sys.argv[1:] or [
        "tests/unit/test_rabbitmq_queue.py",
        "tests/unit/test_robot_status_topic.py",
    ]
    # Pydantic constructs sources before selecting them; block their loaders too.
    with (
        patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}),
        patch.object(EnvSettingsSource, "_load_env_vars", return_value={}),
        patch.object(BaseSettings, "settings_customise_sources", classmethod(isolated_sources)),
    ):
        raise SystemExit(pytest.main(arguments))
