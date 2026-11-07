"""End-to-end smoke test for multi-part uploads.

Run against a live instance (defaults to the local app):

    SD_URL=http://127.0.0.1:8000 SD_PASSWORD=... FOLDER_ID=123 \
    FILE=/tmp/split-test.bin .venv/bin/python tools/split_smoke.py

It uploads FILE, waits for the Telegram job, checks the listing shows one
merged item (with `parts`), downloads it back and compares sha256, then
deletes the uploaded file.
"""
import hashlib
import http.cookiejar
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SD_URL", "http://127.0.0.1:8000").rstrip("/")
PASSWORD = os.environ["SD_PASSWORD"]
FOLDER = int(os.environ["FOLDER_ID"])
PATH = pathlib.Path(os.environ["FILE"])
CHUNK = int(os.environ.get("CHUNK", str(8 * 1024 * 1024)))
HEADERS = {"X-Requested-With": "haodrive"}
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, payload=None, method=None, raw=False, body=None):
    data = body
    headers = dict(HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data is not None else "GET"),
        headers=headers,
    )
    try:
        with opener.open(request, timeout=600) as response:
            content = response.read()
            return response.status, content if raw else json.loads(content or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def wait_job(job_id, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = call(f"/api/jobs/{job_id}")
        if status != 200:
            raise SystemExit(f"job status {status}: {payload}")
        if payload["state"] in ("done", "error"):
            return payload
        time.sleep(1)
    raise SystemExit("job timed out")


def main():
    status, payload = call("/api/login", {"password": PASSWORD})
    if status != 200:
        raise SystemExit(f"login failed: {status} {payload}")

    size = PATH.stat().st_size
    digest = hashlib.sha256()
    with PATH.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    print(f"uploading {PATH.name} ({size} bytes, sha256 {digest.hexdigest()})")

    status, session = call(
        "/api/uploads", {"folder_id": FOLDER, "name": PATH.name, "size": size}
    )
    if status != 200:
        raise SystemExit(f"create session {status}: {session}")

    offset = 0
    with PATH.open("rb") as handle:
        while offset < size:
            piece = handle.read(CHUNK)
            status, payload = call(
                f"/api/uploads/{session['id']}?offset={offset}",
                method="PUT", body=piece,
            )
            if status != 200:
                raise SystemExit(f"chunk at {offset} -> {status}: {payload}")
            offset = payload["offset"]
            print(f"  sent {offset}/{size}", end="\r")
    print()

    status, job = call(f"/api/uploads/{session['id']}/complete", method="POST")
    if status != 200:
        raise SystemExit(f"complete {status}: {job}")
    result = wait_job(job["id"])
    if result["state"] != "done":
        raise SystemExit(f"upload failed: {result.get('error')}")
    print(f"job done: message {result['message_id']}")

    status, listing = call(f"/api/folders/{FOLDER}/files?offset=0&limit=100")
    match = next((item for item in listing["items"] if item["name"] == PATH.name), None)
    if not match:
        raise SystemExit("uploaded file not listed")
    print("listing:", json.dumps(match, ensure_ascii=False))
    if match.get("parts", 1) < 2:
        raise SystemExit(f"expected a multi-part file, got parts={match.get('parts')}")
    if match["size"] != size:
        raise SystemExit(f"merged size {match['size']} != {size}")

    print("downloading merged file...")
    got = hashlib.sha256()
    total = 0
    request = urllib.request.Request(
        BASE + f"/api/folders/{FOLDER}/files/{match['id']}/download", headers=HEADERS
    )
    with opener.open(request, timeout=1800) as response:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            got.update(block)
            total += len(block)
        print(f"  downloaded {total} bytes", end="\r")
    print()
    if total != size or got.hexdigest() != digest.hexdigest():
        raise SystemExit(
            f"content mismatch: {total} bytes, sha256 {got.hexdigest()}"
        )
    print("content OK")

    status, _ = call(f"/api/folders/{FOLDER}/files/{match['id']}", method="DELETE")
    if status != 200:
        raise SystemExit(f"cleanup delete failed: {status}")
    print("cleanup done")


if __name__ == "__main__":
    main()
