import time
from urllib.parse import quote

HEADERS = {"X-Requested-With": "haodrive"}


def _upload(client, folder_id, name, payload):
    created = client.post(
        "/api/uploads",
        json={"folder_id": folder_id, "name": name, "size": len(payload)},
        headers=HEADERS,
    )
    assert created.status_code == 200, created.text
    session = created.json()
    offset = 0
    while offset < len(payload):
        piece = payload[offset:offset + session["chunk_size"]]
        response = client.put(f"/api/uploads/{session['id']}?offset={offset}",
                              content=piece, headers=HEADERS)
        assert response.status_code == 200, response.text
        offset = response.json()["offset"]
    done = client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS)
    assert done.status_code == 200, done.text
    return done.json()


def _wait_job(client, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["state"] in ("done", "error"):
            return payload
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def _file(client, folder_id, name):
    items = client.get(f"/api/folders/{folder_id}/files").json()["items"]
    for item in items:
        if item["name"] == name:
            return item
    raise AssertionError(f"{name} not listed")


def test_chunked_upload_and_listing(client, folder_id):
    payload = b"hello chunk world" * 1000
    job = _upload(client, folder_id, "chunk.txt", payload)
    result = _wait_job(client, job["id"])
    assert result["state"] == "done", result
    assert _file(client, folder_id, "chunk.txt")["size"] == len(payload)


def test_range_head_and_416(client, folder_id):
    payload = bytes(range(256)) * 40
    job = _upload(client, folder_id, "range.bin", payload)
    _wait_job(client, job["id"])
    item = _file(client, folder_id, "range.bin")
    url = f"/api/folders/{folder_id}/files/{item['id']}/stream"

    full = client.get(url)
    assert full.status_code == 200 and full.content == payload
    part = client.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == payload[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(payload)}"
    assert client.get(url, headers={"Range": "bytes=999999-"}).status_code == 416
    head = client.head(url)
    assert head.status_code == 200 and head.headers["content-length"] == str(len(payload))
    assert client.get(url, headers={"Range": "bytes=10-19"}).content == payload[10:20]


def test_share_link_and_password(client, folder_id):
    payload = b"shared bytes"
    job = _upload(client, folder_id, "share.txt", payload)
    _wait_job(client, job["id"])
    item = _file(client, folder_id, "share.txt")
    share = client.post(
        "/api/share",
        json={"folder_id": folder_id, "message_id": item["id"], "hours": 1, "password": "pw123"},
        headers=HEADERS,
    ).json()
    page = client.get(share["url"])
    assert page.status_code == 200 and "密码" in page.text
    assert client.get(f"{share['url']}/raw").status_code == 403
    unlock = client.post(f"{share['url']}/unlock", data={"password": "pw123"},
                         follow_redirects=False)
    assert unlock.status_code == 303
    raw = client.get(f"{share['url']}/raw")
    assert raw.status_code == 200 and raw.content == payload


def test_webdav_propfind_put_get_move_delete(client, folder_id):
    info = client.get("/api/settings/webdav").json()
    auth = (info["user"], info["password"])
    folder_name = client.get("/api/folders").json()["folders"][0]["name"]
    folder_q = quote(folder_name)

    options = client.request("OPTIONS", "/dav/")
    assert options.status_code == 200 and "1" in options.headers.get("dav", "")

    propfind = client.request("PROPFIND", "/dav/", headers={"Depth": "1"}, auth=auth)
    assert propfind.status_code == 207 and folder_name in propfind.text

    url = f"/dav/{folder_q}/dav-test.txt"
    assert client.request("PUT", url, content=b"webdav payload", auth=auth).status_code == 201
    assert client.request("GET", url, auth=auth).content == b"webdav payload"

    moved = client.request(
        "MOVE", url,
        headers={"Destination": f"/dav/{folder_q}/dav-renamed.txt", "Overwrite": "T"},
        auth=auth,
    )
    assert moved.status_code in (201, 204)
    assert client.request("GET", f"/dav/{folder_q}/dav-renamed.txt", auth=auth).status_code == 200
    assert client.request("DELETE", f"/dav/{folder_q}/dav-renamed.txt", auth=auth).status_code == 204


def test_webdav_href_range_and_overwrite(client, folder_id):
    info = client.get("/api/settings/webdav").json()
    auth = (info["user"], info["password"])
    folder_name = client.get("/api/folders").json()["folders"][0]["name"]
    folder_q = quote(folder_name)

    propfind = client.request("PROPFIND", "/dav/", headers={"Depth": "1"}, auth=auth)
    assert f"/dav/{folder_q}/" in propfind.text, "collection href must end with a slash"

    src = f"/dav/{folder_q}/range-src.bin"
    dst = f"/dav/{folder_q}/range-dst.bin"
    assert client.request("PUT", src, content=b"abcdefghij", auth=auth).status_code == 201
    assert client.request("PUT", dst, content=b"zzz", auth=auth).status_code == 201

    part = client.request("GET", src, headers={"Range": "bytes=2-4"}, auth=auth)
    assert part.status_code == 206 and part.content == b"cde"
    assert client.request("GET", src, headers={"Range": "bytes=9999-"}, auth=auth).status_code == 416

    blocked = client.request("MOVE", src,
                             headers={"Destination": dst, "Overwrite": "F"}, auth=auth)
    assert blocked.status_code == 412

    allowed = client.request("MOVE", src,
                             headers={"Destination": dst, "Overwrite": "T"}, auth=auth)
    assert allowed.status_code == 204
    names = [item["name"] for item in
             client.get(f"/api/folders/{folder_id}/files").json()["items"]]
    assert names.count("range-src.bin") == 0
    assert names.count("range-dst.bin") == 1
