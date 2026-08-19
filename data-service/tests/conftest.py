import os

os.environ.setdefault("SESSION_ENCRYPTION_KEY", "wN2t6f5b8t2wG0m1zW2c8r5h4p6q9s3v7y1u3x6a9dQ=")

from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

_ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")

TABLES = ["messages", "sessions", "templates", "leads", "campaigns", "accounts"]


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url()
    return url.replace("postgresql+psycopg2://", "postgresql+psycopg://")


@pytest.fixture(scope="session", autouse=True)
def _configure_settings(database_url: str) -> None:
    os.environ["DATABASE_URL"] = database_url

    from app.config import settings

    settings.DATABASE_URL = database_url

    from app import database

    database.engine = create_engine(database_url, pool_pre_ping=True)
    database.SessionLocal = sessionmaker(
        bind=database.engine, autoflush=False, expire_on_commit=False
    )

    alembic_cfg = Config(_ALEMBIC_INI)
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(autouse=True)
def _clean_tables() -> Generator[None, None, None]:
    yield
    from app.database import engine

    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    from app.main import app

    with TestClient(app) as c:
        yield c
