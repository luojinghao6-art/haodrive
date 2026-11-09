"""Client-side multipart upload bookkeeping and the rolling server spool.

Parts above 10MB never reach this spool: app.stream_upload pushes them to
Telegram while the browser is still sending (see that module).  What is left
here is the legacy rolling spool used by scripted clients, WebDAV and the
small tail part of a sliced upload -- at most one Telegram-sized part sits
under data/tmp at any moment.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import config, db, traffic

GROUP_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def kind_from_mime(mime: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("text/"):
        return "text"
    return "file"


def mime_kind(name: str) -> tuple[str, str]:
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return mime, kind_from_mime(mime)


def group_key_ok(value: str) -> bool:
    return bool(GROUP_KEY_RE.match(value or ""))


def part_sizes(size: int, part_bytes: int) -> list[int]:
    if size <= 0:
        return [0]
    count = (size + part_bytes - 1) // part_bytes
    return [min(part_bytes, size - index * part_bytes) for index in range(count)]


def tmp_usage() -> int:
    total = 0
    try:
        for child in config.TMP_DIR.iterdir():
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def finish_part(group_key: str, part_index: int, message_id: int, part_size: int) -> dict[str, Any] | None:
    """Mark a sent part done and register the file once nothing is missing."""
    db.part_group_set_state(group_key, part_index, "done", message_id)
    registered = db.part_group_finalize(group_key)
    if registered:
        record_logical_upload(registered["folder_id"], registered["size"])
    return registered


def record_logical_upload(folder_id: int, size: int) -> None:
    db.storage_add(folder_id, 1, size)
    traffic.add(up_files=1)


_SPOOL_LOCK = asyncio.Semaphore(1)


async def upload_stream(backend: Any, folder_id: int, name: str,
                        chunks: AsyncIterator[bytes], total_hint: int = 0,
                        progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Consume an HTTP body, rotating through part files, never spooling twice.

    The first full part is held back until a second part actually starts. That
    way a file that fits in one part is sent under its real name, not
    `name.part001`, without ever keeping two parts on disk.
    """
    part_bytes = max(1, config.TELEGRAM_PART_BYTES)
    if tmp_usage() + part_bytes > config.TMP_BUDGET_BYTES and tmp_usage() > 0:
        from .tg_backend import BackendError
        raise BackendError("服务器临时空间不足，等上一个上传完成再试", 507)
    free = shutil.disk_usage(config.TMP_DIR).free
    if free < part_bytes + 64 * 1024 * 1024:
        from .tg_backend import BackendError
        raise BackendError("服务器磁盘空间不足", 507)

    async with _SPOOL_LOCK:
        token = uuid.uuid4().hex
        part_index = 0
        current = 0
        total = 0
        sent: list[dict[str, int]] = []
        pending_first: Path | None = None
        part_path = config.TMP_DIR / f"spool_{token}_{part_index}"
        handle = part_path.open("wb")
        os.chmod(part_path, 0o600)

        def report() -> None:
            if progress:
                progress(total, total_hint or total)

        async def send_part(path: Path, index: int) -> None:
            try:
                info = await backend.upload_part(folder_id, path, name, index, 0)
                sent.append({"id": int(info["id"]), "size": path.stat().st_size})
            finally:
                path.unlink(missing_ok=True)
            report()

        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                position = 0
                while position < len(chunk):
                    room = part_bytes - current
                    take = min(room, len(chunk) - position)
                    handle.write(chunk[position:position + take])
                    traffic.add(up=take)
                    total += take
                    current += take
                    position += take
                    if current < part_bytes:
                        continue
                    handle.close()
                    if part_index == 0:
                        pending_first = part_path
                    else:
                        await send_part(part_path, part_index)
                    part_index += 1
                    current = 0
                    part_path = config.TMP_DIR / f"spool_{token}_{part_index}"
                    handle = part_path.open("wb")
                    os.chmod(part_path, 0o600)
                    if part_index == 1 and pending_first is not None:
                        await send_part(pending_first, 0)
                        pending_first = None
                report()

            handle.close()
            if part_index == 0:
                # Fits in one part: Telegram gets the original filename.
                handle = None
                info = await backend.upload(folder_id, part_path, name, progress)
                part_path.unlink(missing_ok=True)
                record_logical_upload(folder_id, total)
                return info

            if pending_first is not None and not sent:
                # Exactly one full part and nothing after it.
                part_path.unlink(missing_ok=True)
                info = await backend.upload(folder_id, pending_first, name, progress)
                pending_first.unlink(missing_ok=True)
                record_logical_upload(folder_id, total)
                return info

            if current > 0:
                await send_part(part_path, part_index)
            else:
                part_path.unlink(missing_ok=True)
            if not sent:
                from .tg_backend import BackendError
                raise BackendError("上传内容为空", 400)
            mime, kind = mime_kind(name)
            db.group_save(folder_id, sent[0]["id"], name, total, mime, kind, sent)
            record_logical_upload(folder_id, total)
            return await backend.file_info(folder_id, sent[0]["id"])
        except BaseException:
            try:
                handle.close()
            except Exception:
                pass
            part_path.unlink(missing_ok=True)
            if pending_first is not None:
                pending_first.unlink(missing_ok=True)
            raise
