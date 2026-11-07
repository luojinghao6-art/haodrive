import asyncio

from app.media_cache import BLOCK, MediaCache


def test_cache_streams_and_reuses_blocks(tmp_path):
    async def scenario():
        cache = MediaCache(tmp_path / "cache", capacity_bytes=BLOCK * 4)
        size = BLOCK * 2 + 123
        calls = []

        async def fetcher(offset, length):
            calls.append((offset, length))
            data = bytes((index * 7) % 256 for index in range(length))
            for start in range(0, length, 4096):
                yield data[start:start + 4096]

        chunks = []
        async for piece in cache.stream("1_2", size, 0, size, fetcher):
            chunks.append(piece)
        body = b"".join(chunks)
        assert len(body) == size
        first_calls = len(calls)

        chunks = []
        async for piece in cache.stream("1_2", size, 0, 4096, fetcher):
            chunks.append(piece)
        assert b"".join(chunks) == body[:4096]
        assert len(calls) == first_calls, "cached block must not hit Telegram again"
        assert cache.stats()["used"] > 0

    asyncio.run(scenario())


def test_cache_eviction_respects_capacity(tmp_path):
    async def scenario():
        cache = MediaCache(tmp_path / "cache", capacity_bytes=BLOCK * 2)

        async def fetcher(offset, length):
            yield b"x" * length

        for key in ("a", "b", "c", "d"):
            async for _ in cache.stream(key, BLOCK, 0, BLOCK, fetcher):
                pass
        assert cache.stats()["used"] <= BLOCK * 2

    asyncio.run(scenario())


def test_concurrent_streams_return_correct_bytes(tmp_path):
    async def scenario():
        cache = MediaCache(tmp_path / "cache", capacity_bytes=BLOCK * 4)
        size = BLOCK + 100

        async def fetcher(offset, length):
            yield bytes((index + offset) % 256 for index in range(length))

        async def read_all():
            chunks = []
            async for piece in cache.stream("shared", size, 0, size, fetcher):
                chunks.append(piece)
            return b"".join(chunks)

        first, second = await asyncio.gather(read_all(), read_all())
        assert first == second
        assert len(first) == size

    asyncio.run(scenario())


def test_warm_fills_cache_ahead(tmp_path):
    from app.media_cache import PREFETCH_BLOCKS

    async def scenario():
        cache = MediaCache(tmp_path / "cache", capacity_bytes=BLOCK * 16)
        size = BLOCK * 10
        calls = []

        async def fetcher(offset, length):
            calls.append(offset)
            yield bytes(length)

        task = cache.warm("warm", size, fetcher, start=0, max_bytes=BLOCK * 3)
        assert task is not None
        await task
        first = PREFETCH_BLOCKS + 1
        assert calls == [first * BLOCK, (first + 1) * BLOCK, (first + 2) * BLOCK]
        assert cache.stats()["used"] == BLOCK * 3

        calls.clear()
        chunks = [c async for c in cache.stream("warm", size, first * BLOCK, 4096, fetcher)]
        assert b"".join(chunks) == bytes(4096)
        assert calls == [], "warmed blocks must be served from disk"

    asyncio.run(scenario())


def test_partial_range_still_fills_cache(tmp_path):
    async def scenario():
        cache = MediaCache(tmp_path / "cache", capacity_bytes=BLOCK * 4)
        size = BLOCK
        calls = []

        async def fetcher(offset, length):
            calls.append(offset)
            yield bytes(length)

        agen = cache.stream("partial", size, 0, 4096, fetcher)
        first = await agen.__anext__()
        assert len(first) == 4096
        await agen.aclose()  # player aborts after one small range

        for _ in range(100):
            if not cache._tasks:
                break
            await asyncio.sleep(0.01)

        calls.clear()
        chunks = [c async for c in cache.stream("partial", size, 0, 4096, fetcher)]
        assert b"".join(chunks) == bytes(4096)
        assert calls == [], "aborted range must not force a Telegram refetch"

    asyncio.run(scenario())
