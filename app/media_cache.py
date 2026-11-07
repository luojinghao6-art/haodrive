"""Block cache for Telegram media.

Video seeking through MTProto is slow and every range request would otherwise
hit Telegram again. This keeps 4 MiB blocks on local disk, streams a cache miss
to the client while it downloads, and prefetches the next blocks.

Entries and totals are shared between the event loop (stream) and worker
threads (disk reads/writes), so every mutation is guarded by one lock.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

BLOCK = 4 * 1024 * 1024
PREFETCH_BLOCKS = 3
PREFETCH_CONCURRENCY = 2


@dataclass
class Entry:
    key: str
    path: Path
    size: int
    blocks: dict[int, int] = field(default_factory=dict)  # block -> cached bytes
    last_used: float = field(default_factory=time.time)
    active: int = 0

    @property
    def cached_bytes(self) -> int:
        return sum(self.blocks.values())


class MediaCache:
    def __init__(self, root: Path, capacity_bytes: int) -> None:
        self.root = root
        self.capacity = max(capacity_bytes, BLOCK * 2)
        self.entries: dict[str, Entry] = {}
        self.total = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._mutex = threading.RLock()
        self._prefetch_slots = asyncio.Semaphore(PREFETCH_CONCURRENCY)
        self._inflight: set[tuple[str, int]] = set()
        self._block_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        root.mkdir(parents=True, exist_ok=True)
        for child in root.iterdir():
            try:
                if child.is_file():
                    child.unlink()
            except OSError:
                pass

    # ---- paths -----------------------------------------------------------

    def _entry(self, key: str, size: int) -> Entry:
        with self._mutex:
            entry = self.entries.get(key)
            if entry is None or entry.size != size:
                if entry is not None:
                    self.total -= entry.cached_bytes
                    try:
                        entry.path.unlink()
                    except OSError:
                        pass
                safe = key.replace("/", "_")
                entry = Entry(key=key, path=self.root / f"{safe}.bin", size=size)
                entry.path.touch(exist_ok=True)
                self.entries[key] = entry
            entry.last_used = time.time()
            return entry

    def invalidate(self, key: str) -> None:
        with self._mutex:
            entry = self.entries.pop(key, None)
            if not entry:
                return
            self.total -= entry.cached_bytes
            try:
                entry.path.unlink()
            except OSError:
                pass

    def invalidate_prefix(self, prefix: str) -> None:
        with self._mutex:
            for key in [k for k in self.entries if k.startswith(prefix)]:
                entry = self.entries.pop(key, None)
                if not entry:
                    continue
                self.total -= entry.cached_bytes
                try:
                    entry.path.unlink()
                except OSError:
                    pass

    def block_count(self, size: int) -> int:
        return max(1, (size + BLOCK - 1) // BLOCK)

    def block_length(self, size: int, block: int) -> int:
        start = block * BLOCK
        return max(0, min(BLOCK, size - start))

    # ---- disk io ---------------------------------------------------------

    def _read_block(self, entry: Entry, block: int) -> bytes | None:
        with self._mutex:
            if block not in entry.blocks:
                self.misses += 1
                return None
        length = self.block_length(entry.size, block)
        try:
            with entry.path.open("rb") as handle:
                handle.seek(block * BLOCK)
                data = handle.read(length)
        except OSError:
            return None
        if len(data) == length:
            with self._mutex:
                self.hits += 1
            return data
        return None

    def _write_block(self, entry: Entry, block: int, data: bytes) -> bool:
        if len(data) != self.block_length(entry.size, block):
            return False
        try:
            with entry.path.open("r+b") as handle:
                handle.seek(block * BLOCK)
                handle.write(data)
        except OSError:
            return False
        with self._mutex:
            if block not in entry.blocks:
                entry.blocks[block] = len(data)
                self.total += len(data)
            self._evict_locked()
        return True

    # ---- eviction --------------------------------------------------------

    def _evict_locked(self) -> None:
        if self.total <= self.capacity:
            return
        target = self.capacity * 0.8
        for item in sorted(self.entries.values(), key=lambda e: e.last_used):
            if self.total <= target:
                return
            if item.active:
                continue
            try:
                item.path.unlink()
            except OSError:
                pass
            self.total -= item.cached_bytes
            self.entries.pop(item.key, None)
            self.evictions += 1

    # ---- streaming -------------------------------------------------------

    async def stream(
        self,
        key: str,
        size: int,
        start: int,
        length: int,
        fetcher: Callable[[int, int], AsyncIterator[bytes]],
    ) -> AsyncIterator[bytes]:
        entry = self._entry(key, size)
        with self._mutex:
            entry.active += 1
        try:
            position = start
            end = min(size, start + length)
            while position < end:
                block = position // BLOCK
                block_start = block * BLOCK
                block_len = self.block_length(size, block)
                allow_prefetch = length >= 1024 * 1024
                lock = self._block_locks.setdefault((entry.key, block), asyncio.Lock())
                async with lock:
                    cached = await asyncio.to_thread(self._read_block, entry, block)
                    if cached is not None:
                        skip = position - block_start
                        chunk = cached[skip:skip + (end - position)]
                        if chunk:
                            position += len(chunk)
                            yield chunk
                        self._schedule_prefetch(entry, block, fetcher, allow_prefetch)
                        continue

                    skip = position - block_start
                    buffer = bytearray()
                    async for piece in fetcher(block_start, block_len):
                        if not piece:
                            continue
                        piece_start = len(buffer)
                        buffer.extend(piece)
                        if len(buffer) <= skip:
                            continue
                        fresh = max(skip, piece_start)
                        take = min(len(buffer) - fresh, end - position)
                        if take <= 0:
                            if position >= end:
                                break
                            continue
                        out = bytes(buffer[fresh:fresh + take])
                        position += take
                        yield out
                        if position >= end:
                            break
                    if len(buffer) == block_len:
                        await asyncio.to_thread(self._write_block, entry, block, bytes(buffer))
                        self._schedule_prefetch(entry, block, fetcher, allow_prefetch)
                    with self._mutex:
                        entry.last_used = time.time()
        finally:
            with self._mutex:
                entry.active -= 1

    def _schedule_prefetch(
        self,
        entry: Entry,
        block: int,
        fetcher: Callable[[int, int], AsyncIterator[bytes]],
        allow: bool = True,
    ) -> None:
        if not allow:
            return
        total_blocks = self.block_count(entry.size)
        for ahead in range(1, PREFETCH_BLOCKS + 1):
            target = block + ahead
            if target >= total_blocks or target in entry.blocks:
                continue
            marker = (entry.key, target)
            if marker in self._inflight:
                continue
            self._inflight.add(marker)

            async def work(target_block: int = target) -> None:
                try:
                    async with self._prefetch_slots:
                        if target_block in entry.blocks:
                            return
                        if self.entries.get(entry.key) is not entry:
                            return  # invalidated while queued
                        length = self.block_length(entry.size, target_block)
                        buffer = bytearray()
                        async for piece in fetcher(target_block * BLOCK, length):
                            buffer.extend(piece)
                            if len(buffer) >= length:
                                break
                        if len(buffer) >= length:
                            await asyncio.to_thread(
                                self._write_block, entry, target_block, bytes(buffer[:length])
                            )
                except Exception:
                    pass
                finally:
                    self._inflight.discard((entry.key, target_block))

            task = asyncio.create_task(work())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ---- stats -----------------------------------------------------------

    def stats(self) -> dict[str, object]:
        with self._mutex:
            return {
                "entries": len(self.entries),
                "capacity": self.capacity,
                "used": self.total,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "prefetch_inflight": len(self._inflight),
                "root": str(self.root),
            }
