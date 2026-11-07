"""HTTP Range parsing shared by the web API and WebDAV."""
from __future__ import annotations


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return (start, length) or None when no Range header was sent.

    Raises RangeNotSatisfiable for syntactically valid but unsatisfiable
    ranges so callers can answer 416 instead of silently sending the whole
    file.
    """
    if not header:
        return None
    if not header.startswith("bytes=") or size <= 0:
        raise RangeNotSatisfiable()
    spec = header[6:].split(",")[0].strip()
    try:
        if spec.startswith("-"):
            suffix = int(spec[1:])
            if suffix <= 0:
                raise RangeNotSatisfiable()
            start = max(0, size - suffix)
            end = size - 1
        else:
            first, _, last = spec.partition("-")
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        raise RangeNotSatisfiable() from None
    end = min(end, size - 1)
    if start < 0 or start > end or start >= size:
        raise RangeNotSatisfiable()
    return start, end - start + 1


def if_range_matches(header: str | None, etag: str, last_modified: str | None = None) -> bool:
    if not header:
        return True
    value = header.strip()
    if value.startswith('"'):
        return value == etag
    if last_modified:
        return value == last_modified
    return False
