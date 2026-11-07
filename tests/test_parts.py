"""Client-side part upload: grouping, hiding, manifest and range reads."""
import uuid

from app import config

HEADERS = {"X-Requested-With": "haodrive"}


def _send_part(client, folder_id, name, payload, part_bytes, index, count, group_key,
               part_bytes_override=4):
    created = client.post("/api/parts", json={
        "folder_id": folder_id, "name": name, "size": len(payload),
        "part_bytes": part_bytes_override, "part_index": index,
        "part_count": count, "group_key": group_key,
    }, headers=HEADERS)
    assert created.status_code == 200, created.text
    body = created.json()
    if body.get("state") == "done":
        return body
    session = body["id"]
    start = index * part_bytes
    blob = payload[start:start + part_bytes]
    put = client.put(f"/api/uploads/{session}?offset=0", content=blob, headers=HEADERS)
    assert put.status_code == 200, put.text
    done = client.post(f"/api/uploads/{session}/complete", headers=HEADERS)
    assert done.status_code == 200, done.text
    return done.json()


def test_client_parts_register_one_logical_file(client, folder_id, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PART_BYTES", 4)
    payload = b"0123456789"
    name = f"parts-{uuid.uuid4().hex}.bin"
    group = f"g{uuid.uuid4().hex}"
    assert client.get("/api/parts/limits").json()["part_bytes"] == 4

    job = _send_part(client, folder_id, name, payload, 4, 0, 3, group)
    assert job["total"] == 4
    listed = client.get(f"/api/folders/{folder_id}/files?limit=200").json()["items"]
    assert all(item["name"] != name for item in listed), "pending parts must stay hidden"

    _send_part(client, folder_id, name, payload, 4, 1, 3, group)
    _send_part(client, folder_id, name, payload, 4, 2, 3, group)

    listed = client.get(f"/api/folders/{folder_id}/files?limit=200").json()["items"]
    found = next(item for item in listed if item["name"] == name)
    assert found["size"] == 10
    assert found["parts"] == 3

    manifest = client.get(f"/api/folders/{folder_id}/files/{found['id']}/manifest").json()
    assert manifest["size"] == 10
    assert [part["offset"] for part in manifest["parts"]] == [0, 4, 8]
    assert [part["size"] for part in manifest["parts"]] == [4, 4, 2]

    ranged = client.get(
        f"/api/folders/{folder_id}/files/{found['id']}/download",
        headers={"Range": "bytes=2-8"},
    )
    assert ranged.status_code == 206
    assert ranged.content == payload[2:9]

    # The finished part group is gone; the logical file remains.
    assert client.get(f"/api/parts/{group}").status_code == 404
    removed = client.delete(f"/api/folders/{folder_id}/files/{found['id']}", headers=HEADERS)
    assert removed.status_code == 200


def test_single_part_uses_real_name(client, folder_id, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PART_BYTES", 4096)
    payload = b"tiny"
    name = f"one-{uuid.uuid4().hex}.txt"
    group = f"g{uuid.uuid4().hex}"
    _send_part(client, folder_id, name, payload, 4096, 0, 1, group,
               part_bytes_override=4096)

    listed = client.get(f"/api/folders/{folder_id}/files?limit=200").json()["items"]
    found = next(item for item in listed if item["name"] == name)
    assert found["size"] == len(payload)
    assert not found.get("parts") or found["parts"] == 1


def test_cancel_part_group_removes_temp_and_pending(client, folder_id, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PART_BYTES", 8)
    name = f"cancel-{uuid.uuid4().hex}.bin"
    group = f"g{uuid.uuid4().hex}"
    created = client.post("/api/parts", json={
        "folder_id": folder_id, "name": name, "size": 8, "part_bytes": 8,
        "part_index": 0, "part_count": 1, "group_key": group,
    }, headers=HEADERS).json()
    assert created.get("id")
    assert client.get(f"/api/parts/{group}").status_code == 200
    assert client.delete(f"/api/parts/{group}", headers=HEADERS).status_code == 200
    assert client.get(f"/api/parts/{group}").status_code == 404


def test_part_endpoint_rejects_bad_group_key(client, folder_id):
    response = client.post("/api/parts", json={
        "folder_id": folder_id, "name": "x", "size": 10, "part_bytes": 4,
        "part_index": 0, "part_count": 3, "group_key": "no spaces allowed",
    }, headers=HEADERS)
    assert response.status_code == 400
