"""Dashboard counters: storage used and upload/download traffic."""
import uuid

HEADERS = {"X-Requested-With": "haodrive"}


def _upload(client, folder_id, name, payload):
    created = client.post("/api/uploads", json={
        "folder_id": folder_id, "name": name, "size": len(payload),
    }, headers=HEADERS).json()
    session = created["id"]
    put = client.put(f"/api/uploads/{session}?offset=0", content=payload, headers=HEADERS)
    assert put.status_code == 200, put.text
    done = client.post(f"/api/uploads/{session}/complete", headers=HEADERS)
    assert done.status_code == 200, done.text
    return done.json()


def test_stats_storage_and_traffic_move(client, folder_id):
    before = client.get("/api/stats?days=7").json()
    payload = b"traffic-check-" * 100
    name = f"stats-{uuid.uuid4().hex}.txt"
    job = _upload(client, folder_id, name, payload)

    after = client.get("/api/stats?days=7").json()
    assert after["storage"]["used_bytes"] == before["storage"]["used_bytes"] + len(payload)
    assert after["storage"]["files"] == before["storage"]["files"] + 1
    assert after["traffic"]["totals"]["up"] >= before["traffic"]["totals"]["up"] + len(payload)
    assert len(after["traffic"]["days"]) == 7
    assert after["traffic"]["totals"]["up_files"] >= before["traffic"]["totals"]["up_files"] + 1
    assert after["storage"]["by_folder"][0]["bytes"] >= len(payload)

    file_id = job.get("message_id")
    if not file_id:
        listed = client.get(f"/api/folders/{folder_id}/files?limit=200").json()["items"]
        file_id = next(item["id"] for item in listed if item["name"] == name)
    before_down = client.get("/api/stats?days=7").json()["traffic"]["totals"]["down"]
    response = client.get(f"/api/folders/{folder_id}/files/{file_id}/download",
                          headers={"Range": "bytes=0-9"})
    assert response.status_code == 206 and len(response.content) == 10
    after_down = client.get("/api/stats?days=7").json()["traffic"]["totals"]["down"]
    assert after_down >= before_down + 10

    removed_before = client.get("/api/stats?days=7").json()
    assert client.delete(f"/api/folders/{folder_id}/files/{file_id}", headers=HEADERS).status_code == 200
    removed_after = client.get("/api/stats?days=7").json()
    assert removed_after["storage"]["used_bytes"] <= removed_before["storage"]["used_bytes"] - len(payload)


def test_manifest_single_file(client, folder_id):
    payload = b"manifest-single"
    name = f"manifest-{uuid.uuid4().hex}.txt"
    _upload(client, folder_id, name, payload)
    listed = client.get(f"/api/folders/{folder_id}/files?limit=200").json()["items"]
    found = next(item for item in listed if item["name"] == name)
    manifest = client.get(f"/api/folders/{folder_id}/files/{found['id']}/manifest").json()
    assert manifest["size"] == len(payload)
    assert len(manifest["parts"]) == 1
    assert manifest["parts"][0]["offset"] == 0
    assert manifest["parts"][0]["size"] == len(payload)


def test_index_ships_i18n_shell(client):
    html = client.get("/").text
    assert "static/i18n.js" in html
    assert "data-i18n" in html
    assert "stats-page" in html


def test_share_pages_follow_accept_language(client):
    client.cookies.pop("hd_lang", None)
    english = client.get("/s/definitely-not-a-token?lang=en",
                         headers={"Accept-Language": "en-US,en;q=0.9"})
    assert english.status_code == 404
    assert "Link not found" in english.text
    chinese = client.get("/s/definitely-not-a-token?lang=zh",
                         headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    assert "链接不存在" in chinese.text
    forced = client.get("/s/definitely-not-a-token?lang=en",
                        headers={"Accept-Language": "zh-CN"})
    assert "Link not found" in forced.text
