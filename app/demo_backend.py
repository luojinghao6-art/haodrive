"""Demo backend: a local folder that behaves like the Telegram drive, for UI testing."""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import config, db, parts
from .tg_backend import BackendError, UploadJob, _kind_from_mime, plan_parts  # noqa: F401  (see below)


ROOT = config.DATA_DIR / "demo"


def _ensure() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    if not any(ROOT.iterdir()):
        (ROOT / "示例文档.txt").write_text(
            "这是 demo 模式的文件，不连 Telegram。\n删除 DATA_DIR/demo 后重启即可重置。\n",
            encoding="utf-8",
        )


def _describe(folder_id: int, path: Path) -> dict[str, Any]:
    stat = path.stat()
    kind = _kind_from_mime(mimetypes.guess_type(path.name)[0] or "")
    return {
        "id": int(hashlib.sha1(path.name.encode()).hexdigest()[:8], 16),
        "name": path.name,
        "size": stat.st_size,
        "mime": mimetypes.guess_type(path.name)[0] or "",
        "date": int(stat.st_mtime),
        "kind": kind,
        "has_thumb": kind in ("image",),
        "duration": 0,
        "width": 0,
        "height": 0,
        "caption": "",
    }


class DemoBackend:
    name = "demo"
    state = "ready"

    def __init__(self) -> None:
        _ensure()
        self.jobs: dict[str, UploadJob] = {}

    async def start(self) -> None:
        _ensure()

    async def status(self) -> dict[str, Any]:
        return {"state": "ready", "user": {"id": 0, "name": "演示账号", "username": "demo"},
                "error": None, "has_credentials": True, "proxy": False}

    async def begin_login(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise BackendError("demo 模式不支持登录 Telegram", 400)

    async def submit_code(self, code: str) -> dict[str, Any]:
        raise BackendError("demo 模式不支持登录 Telegram", 400)

    async def submit_password(self, password: str) -> dict[str, Any]:
        raise BackendError("demo 模式不支持登录 Telegram", 400)

    async def logout(self) -> None:
        raise BackendError("demo 模式不能退出", 400)

    async def folders(self) -> list[dict[str, Any]]:
        _ensure()
        return [{"id": 1, "name": "演示文件夹", "username": None, "is_public": False}]

    async def create_folder(self, name: str) -> dict[str, Any]:
        return {"id": 1, "name": name, "username": None, "is_public": False}

    async def delete_folder(self, folder_id: int) -> None:
        raise BackendError("demo 模式不能删除文件夹", 400)

    def _path_for(self, message_id: int) -> Path:
        _ensure()
        for path in ROOT.iterdir():
            if path.is_file() and _describe(1, path)["id"] == message_id:
                return path
        raise BackendError("文件不存在", 404)

    async def files(self, folder_id: int, offset: int = 0, limit: int = 60,
                    search: str | None = None) -> dict[str, Any]:
        _ensure()
        paths = sorted((p for p in ROOT.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        items = [_describe(folder_id, p) for p in paths]
        pending = db.pending_part_message_ids(folder_id)
        groups, hidden = db.groups_for_folder(folder_id)
        items = [i for i in items if i["id"] not in pending and i["id"] not in hidden]
        for item in items:
            group = groups.get(item["id"])
            if group:
                item.update({"size": group["size"], "name": group["name"],
                             "mime": group["mime"] or item["mime"],
                             "kind": group["kind"] or item["kind"],
                             "parts": group["parts_count"]})
        names = db.display_names(folder_id, [item["id"] for item in items])
        for item in items:
            if item["id"] in names:
                item["name"] = names[item["id"]]
        if search:
            items = [i for i in items if search.lower() in i["name"].lower()]
        return {"items": items[offset:offset + limit], "next_offset": 0}

    async def file_info(self, folder_id: int, message_id: int) -> dict[str, Any]:
        if message_id in db.pending_part_message_ids(folder_id):
            raise BackendError("文件还没上传完", 404)
        group = db.group_by_message(folder_id, message_id)
        target = group["first_message_id"] if group else message_id
        info = _describe(folder_id, self._path_for(target))
        if group:
            info.update({"size": group["size"], "name": group["name"],
                         "mime": group["mime"] or info["mime"],
                         "kind": group["kind"] or info["kind"],
                         "parts": group["parts_count"]})
        override = db.display_names(folder_id, [target]).get(target)
        if override:
            info["name"] = override
        return info

    async def download_iter(self, folder_id: int, message_id: int, offset: int = 0,
                            length: int | None = None) -> AsyncIterator[bytes]:
        group = db.group_by_message(folder_id, message_id)
        if group and group["parts_count"] > 1:
            parts = db.group_parts(folder_id, group["first_message_id"])
            if length is None:
                length = max(0, group["size"] - offset)
            for part_id, part_offset, take in plan_parts(parts, offset, length):
                path = self._path_for(part_id)
                with path.open("rb") as handle:
                    handle.seek(part_offset)
                    remaining = take
                    while remaining > 0:
                        chunk = handle.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
                        await asyncio.sleep(0)
            return
        path = self._path_for(message_id)
        size = path.stat().st_size
        length = size - offset if length is None else min(length, size - offset)
        with path.open("rb") as handle:
            handle.seek(offset)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
                await asyncio.sleep(0)

    async def thumbnail(self, folder_id: int, message_id: int) -> bytes | None:
        path = self._path_for(message_id)
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            return path.read_bytes()
        return None

    async def delete_messages_only(self, folder_id: int, ids: list[int]) -> None:
        for message_id in ids:
            try:
                self._path_for(message_id).unlink(missing_ok=True)
            except BackendError:
                pass
            db.clear_display_name(folder_id, message_id)

    async def delete_file(self, folder_id: int, message_id: int) -> None:
        group = db.group_by_message(folder_id, message_id)
        if group:
            ids = [p["message_id"] for p in db.group_parts(folder_id, group["first_message_id"])]
            for part_id in ids:
                self._path_for(part_id).unlink(missing_ok=True)
                db.clear_display_name(folder_id, part_id)
            db.group_delete(folder_id, group["first_message_id"])
            db.storage_add(folder_id, -1, -int(group["size"]))
            return
        size = self._path_for(message_id).stat().st_size
        self._path_for(message_id).unlink()
        db.clear_display_name(folder_id, message_id)
        db.storage_add(folder_id, -1, -size)

    async def move_file(self, src_folder: int, message_id: int, dst_folder: int) -> None:
        raise BackendError("demo 模式只有一个文件夹", 400)

    async def copy_file(self, src_folder: int, message_id: int, dst_folder: int) -> None:
        raise BackendError("demo 模式只有一个文件夹", 400)

    async def rename_file(self, folder_id: int, message_id: int, new_name: str) -> dict[str, Any]:
        path = self._path_for(message_id)
        name = Path(new_name).name
        if not name:
            raise BackendError("文件名不能为空")
        db.set_display_name(folder_id, message_id, name)
        info = _describe(folder_id, path)
        info["name"] = name
        return info

    async def upload(self, folder_id: int, path: Path, name: str,
                     progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        _ensure()
        target = ROOT / Path(name).name
        total = path.stat().st_size
        shutil.copyfile(path, target)
        if progress:
            progress(total, total)
        return _describe(folder_id, target)

    async def upload_part(self, folder_id: int, path: Path, name: str,
                          part_index: int, part_count: int = 0,
                          progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        _ensure()
        part_name = name if part_count == 1 else f"{name}.part{part_index + 1:03d}"
        target = ROOT / Path(part_name).name
        total = path.stat().st_size
        shutil.copyfile(path, target)
        if progress:
            progress(total, total)
        return _describe(folder_id, target)

    async def enqueue_upload(self, job: UploadJob) -> UploadJob:
        self.jobs[job.id] = job
        try:
            job.state = "running"
            path = config.TMP_DIR / job.id
            if job.group_key is not None and job.part_index is not None:
                info = await self.upload_part(
                    job.folder_id, path, job.name, job.part_index, job.part_count or 0,
                    lambda sent, total: setattr(job, "sent", sent) or setattr(job, "total", total))
                job.message_id = info["id"]
                parts.finish_part(job.group_key, job.part_index, info["id"], info["size"])
            else:
                info = await self.upload(job.folder_id, path, job.name,
                                         lambda sent, total: setattr(job, "sent", sent) or setattr(job, "total", total))
                job.message_id = info["id"]
                parts.record_logical_upload(job.folder_id, info["size"])
            job.state = "done"
        except Exception as error:
            job.state = "error"
            job.error = str(error)
        finally:
            try:
                os.unlink(config.TMP_DIR / job.id)
            except OSError:
                pass
        return job
