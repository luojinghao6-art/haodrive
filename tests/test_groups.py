"""Multi-part (over 2GB) upload bookkeeping."""
from app import db
from app.tg_backend import _SliceReader, plan_parts


def test_plan_parts_maps_ranges_across_parts():
    parts = [{"id": 1, "size": 100}, {"id": 2, "size": 100}, {"id": 3, "size": 50}]
    assert plan_parts(parts, 0, 10) == [(1, 0, 10)]
    assert plan_parts(parts, 90, 30) == [(1, 90, 10), (2, 0, 20)]
    assert plan_parts(parts, 150, 100) == [(2, 50, 50), (3, 0, 50)]
    assert plan_parts(parts, 250, 10) == []


def test_slice_reader_is_bounded():
    import io

    handle = io.BytesIO(b"abcdefgh")
    handle.seek(2)
    reader = _SliceReader(handle, 3, "x.part001")
    assert reader.read(2) == b"cd"
    assert reader.read(10) == b"e"
    assert reader.read(10) == b""
    assert reader.name == "x.part001"


def test_group_tables_roundtrip():
    db.init_db()
    folder = 900123
    parts = [{"id": 41, "size": 2048}, {"id": 42, "size": 1024}]
    db.group_save(folder, 41, "movie.mkv", 3072, "video/x-matroska", "video", parts)

    group = db.group_get(folder, 41)
    assert group["name"] == "movie.mkv"
    assert group["size"] == 3072
    assert group["parts_count"] == 2

    assert db.group_by_message(folder, 42)["first_message_id"] == 41
    assert [p["message_id"] for p in db.group_parts(folder, 41)] == [41, 42]
    groups, hidden = db.groups_for_folder(folder)
    assert 41 in groups and hidden == {42}
    assert db.search_groups(folder, "mov") == [41]

    db.group_delete(folder, 41)
    assert db.group_by_message(folder, 42) is None


def test_upload_splits_and_registers_group(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import config, db
    from app.tg_backend import TelegramBackend

    monkeypatch.setattr(config, "TELEGRAM_PART_BYTES", 100)
    payload = bytes(range(250))
    path = tmp_path / "big.bin"
    path.write_bytes(payload)

    class FakeClient:
        def __init__(self):
            self.uploads = []
            self.sent = 0

        async def upload_file(self, file, **kwargs):
            data = file.read(kwargs.get("file_size"))
            self.uploads.append((file.name, data))
            return ("handle", file.name, data)

        async def send_file(self, entity, uploaded, **kwargs):
            self.sent += 1
            return SimpleNamespace(id=500 + self.sent)

        async def delete_messages(self, entity, ids):
            pass

    fake = FakeClient()
    backend = TelegramBackend()
    backend._client = lambda: fake

    async def fake_entity(folder_id):
        return object()

    async def fake_message(folder_id, message_id):
        return SimpleNamespace(
            id=message_id,
            file=SimpleNamespace(size=100, mime_type="application/octet-stream",
                                 name="", ext="", duration=0, width=0, height=0),
            photo=None, document=None, date=None, message="",
        )

    backend._entity = fake_entity
    backend._message = fake_message

    db.init_db()
    info = asyncio.run(backend.upload(777001, path, "big.bin"))

    assert fake.sent == 3
    assert [name for name, _ in fake.uploads] == [
        "big.bin.part001", "big.bin.part002", "big.bin.part003"]
    assert b"".join(data for _, data in fake.uploads) == payload
    assert info["parts"] == 3 and info["name"] == "big.bin" and info["size"] == 250

    parts = db.group_parts(777001, 501)
    assert [p["part_size"] for p in parts] == [100, 100, 50]
    group = db.group_by_message(777001, 503)
    assert group["first_message_id"] == 501 and group["size"] == 250
