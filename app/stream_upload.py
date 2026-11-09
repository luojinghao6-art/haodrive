"""Stream one browser part straight into Telegram, without spooling it first.

The old path wrote a whole part (up to 1.9GB) under ``data/tmp`` and only then
called Telethon's ``upload_file`` -- which is a serial for-loop: read 512KB,
send one SaveFilePart, wait for the answer, repeat.  The browser and Telegram
never worked at the same time, and the temp budget capped how much could be
in flight.

Here the HTTP body is cut into 512KB Telegram parts as it arrives and a small
pool of workers pushes them concurrently, so ``max(browser, telegram)`` replaces
``browser + telegram``.  Nothing is spooled: memory use is bounded by
``UPLOAD_BUFFER_BYTES`` and a full buffer simply stops reading the socket.

Resume is expressed in whole Telegram parts.  Only a contiguous run of
confirmed parts counts, because the bytes between the last confirmed part and
the live tail live in memory and die with the process.  That boundary is
persisted to SQLite, so a restart resumes the browser at the same offset.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from . import config, db, parts

logger = logging.getLogger("haodrive.stream")

STREAM_PART = max(1024, int(config.STREAM_PART_BYTES))
# Parts at 10MB or below need a whole-file MD5 (InputFile, not InputFileBig),
# so there is nothing to stream; the spool path handles those tiny tails.
STREAM_MIN_BYTES = int(config.STREAM_MIN_BYTES)
# Do not hammer SQLite once per 512KB part.
PERSIST_EVERY = 16 * 1024 * 1024


class StreamError(Exception):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _Gate:
    """Process-wide cap on concurrent SaveBigFilePart requests."""

    def __init__(self) -> None:
        self._size = max(1, int(config.UPLOAD_CONCURRENCY))
        self._sem = asyncio.Semaphore(self._size)

    def sem(self) -> asyncio.Semaphore:
        size = max(1, int(config.UPLOAD_CONCURRENCY))
        if size != self._size:  # the setting is live
            self._size = size
            self._sem = asyncio.Semaphore(size)
        return self._sem


class _RateLimiter:
    """Token bucket shared by all stream uploads. 0 KB/s means no limit."""

    def __init__(self) -> None:
        self._tokens = 0.0
        self._stamp = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int) -> None:
        rate = max(0, int(config.UPLOAD_RATE_KBPS)) * 1024
        if rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(float(rate), self._tokens + (now - self._stamp) * rate)
                self._stamp = now
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                await asyncio.sleep(max(0.01, (amount - self._tokens) / rate))


GATE = _Gate()
LIMITER = _RateLimiter()


def tg_part_count(size: int) -> int:
    return max(1, (max(0, int(size)) + STREAM_PART - 1) // STREAM_PART)


class StreamUpload:
    """One part (one Telegram document) moving from HTTP to Telegram live."""

    def __init__(self, backend: Any, job: Any, *, upload_id: str, folder_id: int,
                 name: str, size: int, file_id: int, group_key: str | None = None,
                 part_index: int | None = None, part_count: int | None = None,
                 resume_parts: int = 0,
                 on_finished: Callable[[str], None] | None = None) -> None:
        self.backend = backend
        self.job = job
        self.upload_id = upload_id
        self.folder_id = folder_id
        self.name = name
        self.size = max(0, int(size))
        self.file_id = int(file_id)
        self.tg_part_count = tg_part_count(self.size)
        self.group_key = group_key
        self.part_index = part_index
        self.part_count = part_count
        self.on_finished = on_finished

        start = max(0, int(resume_parts)) * STREAM_PART
        self.fed = min(self.size, start)
        self.frontier = self.fed // STREAM_PART
        self.next_index = self.frontier
        self.done_parts: set[int] = set()
        self.buffer = bytearray()
        capacity = max(2, int(config.UPLOAD_BUFFER_BYTES) // STREAM_PART)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        self.state = "uploading"  # uploading | finishing | done | error | cancelled
        self.error: StreamError | None = None
        self.pump: asyncio.Task | None = None
        self.tasks: list[asyncio.Task] = []
        self._persisted = self.fed

    # ---- introspection ---------------------------------------------------

    @property
    def confirmed_bytes(self) -> int:
        """Bytes the browser may skip on resume: contiguous confirmed parts."""
        return min(self.size, self.frontier * STREAM_PART)

    def status(self) -> dict[str, Any]:
        return {"offset": self.fed, "sent": self.confirmed_bytes, "state": self.state,
                "error": self.error.message if self.error else None}

    # ---- HTTP side -------------------------------------------------------

    async def feed(self, data: bytes) -> None:
        if not data:
            return
        if self.state != "uploading":
            raise StreamError("这次上传已经不再接收数据了", 409)
        if self.fed + len(data) > self.size:
            raise StreamError("超过声明的分片大小", 413)
        self._start()
        if self.error is not None:
            raise self.error
        self.buffer.extend(data)
        while len(self.buffer) >= STREAM_PART:
            block = bytes(self.buffer[:STREAM_PART])
            del self.buffer[:STREAM_PART]
            await self._enqueue(block)
        # Only now is the data really queued, so this is the offset the
        # browser may safely continue from on this session.
        self.fed += len(data)

    async def finish(self) -> dict[str, Any]:
        if self.state in ("finishing", "done"):
            return {"id": self.job.id, "name": self.job.name, "total": self.job.total}
        if self.state == "error" and self.error is not None:
            raise self.error
        if self.state == "cancelled":
            raise StreamError("上传已取消", 409)
        if self.fed != self.size:
            raise StreamError(f"还差 {self.size - self.fed} 字节", 409)
        self._start()
        if self.buffer:
            await self._enqueue(bytes(self.buffer))
            self.buffer.clear()
        self.state = "finishing"
        self.job.state = "running"
        await self.queue.put(None)
        return {"id": self.job.id, "name": self.job.name, "total": self.job.total}

    async def cancel(self) -> None:
        if self.state in ("done", "cancelled"):
            return
        self.state = "cancelled"
        self._drain()
        if self.pump is not None:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            db.upload_delete(self.upload_id)
        except Exception:
            logger.exception("could not drop cancelled upload %s", self.upload_id)
        self._finished()

    # ---- pump ------------------------------------------------------------

    def _start(self) -> None:
        if self.pump is not None and not self.pump.done():
            return
        self.pump = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    break
                sem = GATE.sem()
                await sem.acquire()
                if self.error is not None:
                    sem.release()
                    break
                index, block = item
                task = asyncio.create_task(self._send(index, block))
                # Release from a done callback: a task cancelled before its
                # first step never enters _send, so its finally would not run
                # and the global slot would leak forever.
                task.add_done_callback(lambda _task, gate=sem: gate.release())
                self.tasks.append(task)
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.error is not None:
                raise self.error
            await self._send_message()
            self._persist(force=True)
            try:
                db.upload_delete(self.upload_id)
            except Exception:
                logger.exception("could not clear finished upload %s", self.upload_id)
            self.state = "done"
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - every failure belongs to the job
            self._fail(error)
        finally:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            self._finished()

    async def _send(self, index: int, block: bytes) -> None:
        try:
            if self.error is not None:
                return
            await LIMITER.acquire(len(block))
            if self.error is not None:
                return
            for attempt in range(3):
                try:
                    await self.backend.save_big_part(
                        self.file_id, index, self.tg_part_count, block)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == 2 or self.error is not None:
                        raise
                    # A part is idempotent, so a transient flood/network error
                    # only costs one retry, not the whole upload.
                    await asyncio.sleep(min(0.5 * 2 ** attempt, 5))
            self.done_parts.add(index)
            self._advance()
            self.job.sent = min(self.size, len(self.done_parts) * STREAM_PART)
            self._persist()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(error)

    async def _send_message(self) -> None:
        single = (self.part_count or 1) == 1
        if single:
            part_name, caption = self.name, ""
        else:
            part_name = f"{self.name}.part{(self.part_index or 0) + 1:03d}"
            caption = f"[TD-PART {(self.part_index or 0) + 1}/{self.part_count}]"
        info = await self.backend.send_uploaded(
            self.folder_id, self.file_id, self.tg_part_count, part_name, caption)
        self.job.message_id = int(info["id"])
        self.job.sent = self.job.total or self.size
        self.job.state = "done"
        self.job.error = None
        db.job_save(self.job)
        if self.group_key is not None and self.part_index is not None:
            parts.finish_part(self.group_key, self.part_index, int(info["id"]), int(info["size"]))
        else:
            parts.record_logical_upload(self.folder_id, int(info["size"]))
        logger.info("streamed part %s/%s done: %s -> message %s",
                    (self.part_index or 0) + 1, self.part_count or "?",
                    self.name, info["id"])

    # ---- internals -------------------------------------------------------

    async def _enqueue(self, block: bytes) -> None:
        if self.error is not None:
            raise self.error
        index = self.next_index
        self.next_index += 1
        await self.queue.put((index, block))

    def _advance(self) -> None:
        while self.frontier in self.done_parts:
            self.frontier += 1

    def _persist(self, force: bool = False) -> None:
        confirmed = self.confirmed_bytes
        if not force and confirmed - self._persisted < PERSIST_EVERY:
            return
        if confirmed == self._persisted and not force:
            return
        self._persisted = confirmed
        try:
            db.upload_set_confirmed(self.upload_id, confirmed)
        except Exception:
            logger.exception("could not persist upload offset for %s", self.upload_id)

    def _drain(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def _fail(self, error: Exception) -> None:
        if self.error is not None:
            return
        message = getattr(error, "message", None) or str(error) or "上传失败"
        self.error = StreamError(str(message), int(getattr(error, "code", 400) or 400))
        self.state = "error"
        self.job.state = "error"
        self.job.error = str(message)
        self._persist(force=True)
        text = str(message).upper()
        if "FILE_PART" in text or "FILE_ID_INVALID" in text:
            # Telegram threw the earlier parts away; a retry must start over.
            try:
                db.upload_set_confirmed(self.upload_id, 0)
            except Exception:
                logger.exception("could not reset upload offset for %s", self.upload_id)
        try:
            db.job_save(self.job)
        except Exception:
            logger.exception("could not save failed upload %s", self.upload_id)
        self._drain()
        logger.warning("stream upload failed for %s: %s", self.name, message)

    def _finished(self) -> None:
        if self.on_finished is not None:
            try:
                self.on_finished(self.upload_id)
            except Exception:
                logger.exception("stream cleanup callback failed")
