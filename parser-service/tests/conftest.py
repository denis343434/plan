import os

os.environ.setdefault("DATA_SERVICE_URL", "http://data-service.test")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tasks import TASKS


@pytest.fixture(autouse=True)
def _clear_tasks():
    TASKS.clear()
    yield
    TASKS.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
