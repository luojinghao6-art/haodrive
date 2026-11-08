"""End-to-end check: multipart upload -> merged playback across part boundaries.

Uploads a real file through the browser-style part API, then reads it back with
the same HTTP Range pattern a <video> element uses, and verifies that every byte
matches the source. Optionally keeps the uploaded file for a manual look.

Usage (demo or real instance):
    python tools/playback_test.py --base http://127.0.0.1:8010 --password demo \
        --file sample.mp4 --part-bytes 1048576

The script prints one line per phase and exits non-zero if any check fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

MUTATION = {"X-Requested-With": "haodrive"}


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, data: bytes | None = None,
                headers: dict[str, str] | None = None,
                timeout: float = 120) -> tuple[int, dict[str, str], bytes]:
        url = self.base + path
        req = urllib.request.Request(url, data=data, method=method)
        if method in ("POST", "PUT", "DELETE"):
            for key, value in MUTATION.items():
                req.add_header(key, value)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            return error.code, {k.lower(): v for k, v in error.headers.items()}, body

    def json(self, method: str, path: str, payload: dict | None = None,
             timeout: float = 120) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else None
        status, _, body = self.request(method, path, data, headers, timeout)
        try:
            return status, json.loads(body or b"{}")
        except ValueError:
            return status, {"raw": body.decode(errors="replace")}

    def stream(self, path: str, headers: dict[str, str],
               timeout: float = 300):
        req = urllib.request.Request(self.base + path, headers=headers)
        return self.opener.open(req, timeout=timeout)


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def upload_multipart(client: Client, folder_id: int, path: str,
                     part_bytes: int) -> tuple[str, int, list[dict]]:
    size = os.path.getsize(path)
    name = os.path.basename(path)
    part_count = max(1, (size + part_bytes - 1) // part_bytes)
    group_key = secrets.token_hex(16)
    print(f"[upload] {name} {size} bytes, part_bytes={part_bytes}, parts={part_count}")
    started = time.time()
    for index in range(part_count):
        start = index * part_bytes
        take = min(part_bytes, size - start)
        status, info = client.json("POST", "/api/parts", {
            "folder_id": folder_id, "name": name, "size": size,
            "part_bytes": part_bytes, "group_key": group_key,
            "part_index": index, "part_count": part_count,
        })
        if status != 200:
            fail(f"create part {index}: {status} {info}")
        if info.get("state") == "done":
            print(f"  part {index}: already on server")
            continue
        upload_id = info["id"]
        chunk_size = int(info.get("chunk_size") or 8 * 1024 * 1024)
        offset = int(info.get("offset") or 0)
        part_started = time.time()
        with open(path, "rb") as handle:
            handle.seek(start + offset)
            while offset < take:
                piece = handle.read(min(chunk_size, take - offset))
                if not piece:
                    fail("source file truncated")
                status, _, body = client.request(
                    "PUT", f"/api/uploads/{upload_id}?offset={offset}", piece,
                    {"Content-Type": "application/octet-stream"}, timeout=600)
                if status != 200:
                    fail(f"upload bytes at {offset}: {status} {body[:200]!r}")
                offset = int(json.loads(body)["offset"])
        status, job = client.json("POST", f"/api/uploads/{upload_id}/complete")
        if status != 200:
            fail(f"complete part {index}: {status} {job}")
        job_id = job["id"]
        deadline = time.time() + 900
        while True:
            status, state = client.json("GET", f"/api/jobs/{job_id}")
            if status != 200:
                fail(f"job {job_id}: {status} {state}")
            if state.get("state") == "done":
                break
            if state.get("state") == "error":
                fail(f"part {index} telegram send failed: {state.get('error')}")
            if time.time() > deadline:
                fail(f"part {index} timed out")
            time.sleep(1)
        print(f"  part {index}: {take} bytes sent in {time.time() - part_started:.1f}s "
              f"-> message {state.get('message_id')}")
    deadline = time.time() + 60
    group = {}
    while True:
        status, group = client.json("GET", f"/api/parts/{group_key}")
        if status == 404:
            # A finalized group is deleted from the bookkeeping table: its file
            # row now owns the part list.
            print("  group finalized (bookkeeping row removed)")
            break
        if status == 200 and all(p["state"] == "done" for p in group["parts"]):
            break
        if time.time() > deadline:
            fail(f"group not finalized: {group}")
        time.sleep(1)
    print(f"[upload] done in {time.time() - started:.1f}s")
    return group_key, size, []


def find_file(client: Client, folder_id: int, name: str, size: int) -> dict:
    status, listing = client.json("GET", f"/api/folders/{folder_id}/files?limit=200")
    if status != 200:
        fail(f"list files: {status} {listing}")
    for item in listing.get("items", []):
        if item.get("name") == name and int(item.get("size", 0)) == size:
            return item
    fail(f"uploaded file {name} not in listing")
    raise AssertionError


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(client: Client, folder_id: int, message_id: int, path: str) -> dict:
    source = open(path, "rb").read()
    size = len(source)
    base = f"/api/folders/{folder_id}/files/{message_id}"

    status, manifest = client.json("GET", f"{base}/manifest")
    if status != 200:
        fail(f"manifest: {status} {manifest}")
    parts = manifest["parts"]
    position = 0
    for index, part in enumerate(parts):
        if int(part["offset"]) != position:
            fail(f"part {index} offset {part['offset']} != {position}")
        position += int(part["size"])
    if position != size:
        fail(f"parts sum {position} != file size {size}")
    print(f"[manifest] {len(parts)} parts, sizes={[p['size'] for p in parts]}")

    req = urllib.request.Request(client.base + f"{base}/stream", method="HEAD")
    status, headers, _ = client.request("HEAD", f"{base}/stream")
    if status != 200:
        fail(f"HEAD stream: {status}")
    if headers.get("accept-ranges") != "bytes":
        fail("stream does not advertise Accept-Ranges: bytes")
    if int(headers.get("content-length", -1)) != size:
        fail(f"HEAD length {headers.get('content-length')} != {size}")
    print(f"[head] 200, Content-Length={size}, Accept-Ranges=bytes")

    # A player asks for a range; it must come back as 206 with exact bytes.
    chunk = 1024 * 1024
    received = bytearray()
    ranges: list[tuple[int, int]] = []
    started = time.time()
    position = 0
    while position < size:
        end = min(size - 1, position + chunk - 1)
        response = client.stream(f"{base}/stream",
                                 {"Range": f"bytes={position}-{end}"})
        if response.status != 206:
            fail(f"range {position}-{end}: status {response.status}")
        content_range = response.headers.get("content-range", "")
        expected = f"bytes {position}-{end}/{size}"
        if content_range != expected:
            fail(f"Content-Range {content_range!r} != {expected!r}")
        data = response.read()
        if len(data) != end - position + 1:
            fail(f"range {position}-{end}: got {len(data)} bytes")
        if data != source[position:end + 1]:
            fail(f"range {position}-{end}: bytes differ from source")
        received.extend(data)
        ranges.append((position, end))
        position = end + 1
    elapsed = time.time() - started
    if sha256(bytes(received)) != sha256(source):
        fail("sequential playback hash mismatch")
    print(f"[play] {len(ranges)} sequential range requests, {size} bytes, "
          f"{elapsed:.2f}s, sha256 ok")

    # Ranges that start in one part and end in another.
    position = 0
    for part in parts[:-1]:
        position += int(part["size"])
        start = max(0, position - 65536)
        end = min(size - 1, position + 65536)
        response = client.stream(f"{base}/stream", {"Range": f"bytes={start}-{end}"})
        data = response.read()
        if response.status != 206 or data != source[start:end + 1]:
            fail(f"boundary range {start}-{end} mismatch")
    print(f"[boundary] {len(parts) - 1} cross-part ranges ok")

    # Seek straight into the middle of a later part.
    target = int(size * 0.75)
    end = min(size - 1, target + 65535)
    response = client.stream(f"{base}/stream", {"Range": f"bytes={target}-{end}"})
    data = response.read()
    if response.status != 206 or data != source[target:end + 1]:
        fail("seek range mismatch")
    print(f"[seek] bytes={target}-{end} ok")

    # Plain download (no Range) must also stitch every part.
    status, headers, body = client.request("GET", f"{base}/download", timeout=900)
    if status != 200 or len(body) != size or body != source:
        fail(f"download mismatch: status={status} size={len(body)}")
    print(f"[download] {size} bytes, sha256 ok")
    return {"parts": len(parts), "requests": len(ranges), "seconds": round(elapsed, 2),
            "sha256": sha256(source)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--part-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--folder", type=int, default=0)
    parser.add_argument("--keep", action="store_true",
                        help="keep the uploaded file (default: delete afterwards)")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    client = Client(args.base)
    status, body = client.json("POST", "/api/login", {"password": args.password})
    if status != 200:
        fail(f"login: {status} {body}")
    print("[login] ok")

    folder_id = args.folder
    if not folder_id:
        status, folders = client.json("GET", "/api/folders")
        if status != 200 or not folders.get("folders"):
            fail(f"folders: {status} {folders}")
        folder_id = int(folders["folders"][0]["id"])
    print(f"[folder] id={folder_id}")

    group_key, size, _ = upload_multipart(client, folder_id, args.file, args.part_bytes)
    name = os.path.basename(args.file)
    item = find_file(client, folder_id, name, size)
    message_id = int(item["id"])
    print(f"[file] message_id={message_id} parts={item.get('parts')}")

    report = verify(client, folder_id, message_id, args.file)
    report.update({"file": name, "size": size, "message_id": message_id,
                   "group_key": group_key})

    if args.keep:
        print(f"[keep] left on server: {name} (message {message_id})")
    else:
        status, body = client.json("DELETE", f"/api/folders/{folder_id}/files/{message_id}")
        if status != 200:
            print(f"WARN: delete failed: {status} {body}")
        else:
            print("[cleanup] test file deleted")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    print("RESULT " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
