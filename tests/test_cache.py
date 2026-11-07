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
