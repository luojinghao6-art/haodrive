import os
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="seagulldrive-test-")
os.environ["DEMO_MODE"] = "1"
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["DATA_DIR"] = DATA_DIR
os.environ["APP_SECRET"] = "test-app-secret-0123456789"

import pytest
from fastapi.testclient import TestClient

from app.main import app

HEADERS = {"X-Requested-With": "seagulldrive"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        response = test_client.post("/api/login", json={"password": "test-password-123"}, headers=HEADERS)
        assert response.status_code == 200
        yield test_client


@pytest.fixture()
def folder_id(client):
    data = client.get("/api/folders").json()
    return data["folders"][0]["id"]
