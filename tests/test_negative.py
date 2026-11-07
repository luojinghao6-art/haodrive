import pytest
from fastapi.testclient import TestClient
from app.main import app

HEADERS = {"X-Requested-With": "haodrive"}


@pytest.fixture()
def anon():
    with TestClient(app) as client:
        yield client


def test_api_requires_session(anon):
    assert anon.get("/api/folders").status_code == 401
    assert anon.post("/api/folders", json={"name": "x"}, headers=HEADERS).status_code == 401


def test_mutation_requires_custom_header(client):
    # Authenticated but without the CSRF-ish header.
    assert client.post("/api/folders", json={"name": "x"}).status_code == 403


def test_logout_requires_session(anon):
    assert anon.post("/api/logout", headers=HEADERS).status_code == 401


def test_webdav_rejects_bad_credentials(client):
    response = client.request("PROPFIND", "/dav/", headers={"Depth": "0"}, auth=("nobody", "wrong"))
    assert response.status_code == 401


def test_webdav_depth_infinity_refused(client):
    info = client.get("/api/settings/webdav").json()
    response = client.request("PROPFIND", "/dav/", headers={"Depth": "infinity"},
                              auth=(info["user"], info["password"]))
    assert response.status_code == 403


def test_upload_offset_conflict(client, folder_id):
    created = client.post("/api/uploads",
                          json={"folder_id": folder_id, "name": "conflict.bin", "size": 10},
                          headers=HEADERS).json()
    response = client.put(f"/api/uploads/{created['id']}?offset=5", content=b"x" * 5, headers=HEADERS)
    assert response.status_code == 409
    client.delete(f"/api/uploads/{created['id']}", headers=HEADERS)


def test_upload_rejects_oversize(client, folder_id):
    response = client.post("/api/uploads",
                           json={"folder_id": folder_id, "name": "big.bin", "size": 10**12},
                           headers=HEADERS)
    assert response.status_code == 413
