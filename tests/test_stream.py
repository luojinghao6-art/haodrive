"""Streaming upload: parts go to Telegram while the browser is still sending."""
import asyncio
import time
import uuid

import pytest

from app import config, db
from app import main as main_module
from app.stream_upload import STREAM_PART, StreamError, StreamUpload
from app.tg_backend import BackendError, UploadJob

HEADERS = {"X-Requested-With": "haodrive"}


@pytest.fixture(autouse=True)
def _database():
    """These tests create jobs without going through the app's startup hook."""
    db.init_db()
    yield


class FakeBackend:
    """Stands in for Telegram: records SaveBigFilePart calls and send_file."""

    supports_streaming = True

    def __init__(self, fail_at: int | None = None) -> None:
        self.saved: dict[int, bytes] = {}
        self.attachment: list[int] = []
        self.sent: tuple | None = None
        self.fail_at = fail_at
        self.inflight = 0
        self.peak_inflight = 0
        self.jobs: dict = {}

    async def save_big_part(self, file_id: int, index: int, count: int, data: bytes) -> None:
        self.inflight += 1
        self.peak_inflight = max(self.peak_inflight, self.inflight)
        try:
            await asyncio.sleep(0.01)
            if self.fail_at == index:
                raise BackendError("Telegram 炸了")
            self.saved[index] = data
            self.attachment.append(index)
        finally:
            self.inflight -= 1

    async def send_uploaded(self, folder_id: int, file_id: int, count: int,
                            part_name: str, caption: str) -> dict:
        self.sent = (folder_id, file_id, count, part_name, caption)
        return {"id": 4242, "size": sum(len(part) for part in self.saved.values())}


def _make(backend, size, upload_id=None, resume_parts=0, name="big.bin"):
    upload_id = upload_id or uuid.uuid4().hex
    job = UploadJob(id=upload_id, name=name, folder_id=1, total=size)
    upload = StreamUpload(backend, job, upload_id=upload_id, folder_id=1, name=name,
                          size=size, file_id=777, resume_parts=resume_parts)
    return upload, job


async def _drive(upload, payload, chunk=200_000):
    for start in range(0, len(payload), chunk):
        await upload.feed(payload[start:start + chunk])
    await upload.finish()
    if upload.pump is not None:
        await upload.pump


def test_stream_sends_parts_without_spooling(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_CONCURRENCY", 4)
    monkeypatch.setattr(config, "UPLOAD_RATE_KBPS", 0)
    size = STREAM_PART * 3 + 1000
    payload = bytes((index * 7) % 251 for index in range(size))
    backend = FakeBackend()
    upload, job = _make(backend, size)

    asyncio.run(_drive(upload, payload))

    assert job.state == "done"
    assert job.message_id == 4242
    assert sorted(backend.saved) == [0, 1, 2, 3]
    assert backend.saved[3] == payload[STREAM_PART * 3:]
    assert b"".join(backend.saved[index] for index in sorted(backend.saved)) == payload
    assert backend.sent[2] == 4  # 4 x 512KB parts
    assert backend.peak_inflight > 1, "parts must overlap, not trickle one at a time"
    assert upload.state == "done"
    assert upload.confirmed_bytes == size


def test_failure_keeps_confirmed_boundary_for_resume(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_CONCURRENCY", 2)
    upload_id = uuid.uuid4().hex
    size = STREAM_PART * 3
    db.upload_save(upload_id, 1, "big.bin", size, 0, int(time.time()),
                   "g" + uuid.uuid4().hex[:16], 0, stream=1, file_id=777, confirmed=0)
    backend = FakeBackend(fail_at=1)
    payload = b"x" * size
    upload, job = _make(backend, size, upload_id=upload_id)

    asyncio.run(_drive(upload, payload))

    assert job.state == "error"
    assert "Telegram" in (job.error or "")
    # Only part 0 is a contiguous confirmed run; part 2 must not be counted.
    assert upload.confirmed_bytes == STREAM_PART
    rows = {row["id"]: row for row in db.upload_load_all()}
    assert rows[upload_id]["confirmed"] == STREAM_PART

    # A fresh attempt resumes exactly at the confirmed boundary.
    backend.fail_at = None
    resumed, job2 = _make(backend, size, upload_id=upload_id, resume_parts=1)
    assert resumed.fed == STREAM_PART
    asyncio.run(_drive(resumed, payload[STREAM_PART:]))
    assert job2.state == "done"
    assert sorted(backend.saved) == [0, 1, 2]


def test_stream_rejects_overflow_and_early_finish():
    size = STREAM_PART
    backend = FakeBackend()
    upload, _ = _make(backend, size)

    with pytest.raises(StreamError) as overflow:
        asyncio.run(upload.feed(b"y" * (size + 1)))
    assert overflow.value.code == 413

    with pytest.raises(StreamError) as early:
        asyncio.run(upload.finish())
    assert early.value.code == 409


def test_feed_blocks_while_telegram_is_behind(monkeypatch):
    """A full buffer must stop reading the browser instead of growing."""
    monkeypatch.setattr(config, "UPLOAD_BUFFER_BYTES", STREAM_PART * 2)
    monkeypatch.setattr(config, "UPLOAD_CONCURRENCY", 1)
    release = asyncio.Event()

    class Stalled(FakeBackend):
        async def save_big_part(self, file_id, index, count, data):
            await release.wait()
            await super().save_big_part(file_id, index, count, data)

    async def scenario():
        upload, _ = _make(Stalled(), STREAM_PART * 10)
        upload._start()
        # Two blocks fit in the queue, one more is held by the stalled pump;
        # the rest has nowhere to go, so the feed has to wait.
        task = asyncio.create_task(upload.feed(b"z" * (STREAM_PART * 8)))
        await asyncio.sleep(0.05)
        assert not task.done(), "feed must block once the buffer is full"
        release.set()
        await task
        await upload.cancel()

    asyncio.run(scenario())


def test_api_stream_part_end_to_end(client, folder_id, monkeypatch):
    """The HTTP endpoints hand bytes to Telegram while the body is arriving."""
    backend = FakeBackend()
    monkeypatch.setattr(main_module, "backend", backend)
    monkeypatch.setattr(main_module, "STREAM_MIN_BYTES", 1024)
    monkeypatch.setattr(config, "UPLOAD_CONCURRENCY", 2)

    payload = b"stream-me-" * 200
    size = len(payload)
    part_bytes = size
    name = f"stream-{uuid.uuid4().hex}.bin"
    group = "g" + uuid.uuid4().hex

    created = client.post("/api/parts", json={
        "folder_id": folder_id, "name": name, "size": size,
        "part_bytes": part_bytes, "part_index": 0, "part_count": 1,
        "group_key": group,
    }, headers=HEADERS)
    assert created.status_code == 200, created.text
    session = created.json()
    assert session["stream"] is True
    assert session["offset"] == 0

    put = client.put(f"/api/uploads/{session['id']}?offset=0", content=payload, headers=HEADERS)
    assert put.status_code == 200, put.text
    assert put.json()["offset"] == size

    done = client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS)
    assert done.status_code == 200, done.text
    job_id = done.json()["id"]

    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "done", job
    assert b"".join(backend.saved[index] for index in sorted(backend.saved)) == payload
    assert backend.sent[3] == name  # single part keeps its real name


def test_api_retry_resumes_at_confirmed_part(client, folder_id, monkeypatch):
    """A failed stream restarts at the last part Telegram confirmed."""
    backend = FakeBackend(fail_at=1)
    monkeypatch.setattr(main_module, "backend", backend)
    monkeypatch.setattr(main_module, "STREAM_MIN_BYTES", 1024)
    monkeypatch.setattr(config, "UPLOAD_CONCURRENCY", 1)

    size = STREAM_PART * 2 + 100
    payload = bytes((index * 11) % 251 for index in range(size))
    name = f"retry-{uuid.uuid4().hex}.bin"
    group = "g" + uuid.uuid4().hex

    def create():
        response = client.post("/api/parts", json={
            "folder_id": folder_id, "name": name, "size": size,
            "part_bytes": size, "part_index": 0, "part_count": 1,
            "group_key": group,
        }, headers=HEADERS)
        assert response.status_code == 200, response.text
        return response.json()

    first = create()
    assert first["offset"] == 0
    put = client.put(f"/api/uploads/{first['id']}?offset=0", content=payload, headers=HEADERS)
    assert put.status_code == 200, put.text
    assert client.post(f"/api/uploads/{first['id']}/complete", headers=HEADERS).status_code == 200

    for _ in range(100):
        job = client.get(f"/api/jobs/{first['id']}").json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "error"
    assert backend.saved and 0 in backend.saved

    second = create()
    assert second["id"] != first["id"]
    assert second["offset"] == STREAM_PART, "must not re-send confirmed parts"
    assert second["stream"] is True

    backend.fail_at = None
    rest = payload[STREAM_PART:]
    put = client.put(f"/api/uploads/{second['id']}?offset={STREAM_PART}", content=rest,
                     headers=HEADERS)
    assert put.status_code == 200, put.text
    assert client.post(f"/api/uploads/{second['id']}/complete", headers=HEADERS).status_code == 200
    for _ in range(100):
        job = client.get(f"/api/jobs/{second['id']}").json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "done", job
    assert b"".join(backend.saved[index] for index in sorted(backend.saved)) == payload


def test_restart_restores_session_at_confirmed_boundary():
    """A server restart must resume the browser, not restart the part."""
    group = "g" + uuid.uuid4().hex
    upload_id = uuid.uuid4().hex
    size = STREAM_PART * 4
    name = f"restart-{uuid.uuid4().hex}.bin"
    db.part_group_create(group, 1, name, size, size, 1, "application/octet-stream",
                         "file", [size])
    db.upload_save(upload_id, 1, name, size, STREAM_PART * 2, int(time.time()),
                   group, 0, stream=1, file_id=987654321, confirmed=STREAM_PART * 2)
    row = db.upload_get(upload_id)
    assert row is not None

    main_module._restore_stream(row, time.time())

    session = main_module.UPLOAD_SESSIONS[upload_id]
    assert session["received"] == STREAM_PART * 2
    assert session["upload"].file_id == 987654321
    assert session["upload"].tg_part_count == 4
    assert session["upload"].state == "uploading"
    main_module._drop_stream_session(upload_id)
    db.part_group_delete(group)
    db.upload_delete(upload_id)


def test_transient_part_failure_is_retried(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_CONCURRENCY", 2)

    class Flaky(FakeBackend):
        def __init__(self):
            super().__init__()
            self.failed_once = False

        async def save_big_part(self, file_id, index, count, data):
            if index == 1 and not self.failed_once:
                self.failed_once = True
                raise BackendError("FLOOD_WAIT_1")
            await super().save_big_part(file_id, index, count, data)

    backend = Flaky()
    size = STREAM_PART * 3
    payload = b"q" * size
    upload, job = _make(backend, size)

    asyncio.run(_drive(upload, payload))

    assert job.state == "done"
    assert backend.failed_once is True
    assert sorted(backend.saved) == [0, 1, 2]
