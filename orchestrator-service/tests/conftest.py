import os

os.environ.setdefault("DATA_SERVICE_URL", "http://data-service.test")
os.environ.setdefault("PARSER_SERVICE_URL", "http://parser-service.test")
os.environ.setdefault("MESSAGING_SERVICE_URL", "http://messaging-service.test")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orchestration import TASKS


@pytest.fixture(autouse=True)
def _clear_tasks():
    TASKS.clear()
    yield
    TASKS.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
