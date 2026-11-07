"""WebDAV front-end for the Telegram drive.

Mounted at /dav (after Caddy strips the secret prefix, the client still sees
the full path and we echo it back in hrefs via X-Forwarded-Prefix).

Supported: OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, MOVE,
COPY, LOCK, UNLOCK. Folder = channel, file = message.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from . import config, db, parts
from .http_range import RangeNotSatisfiable, parse_range
from .tg_backend import BackendError

router = APIRouter()

PROPFIND_XML = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">{body}</D:multistatus>"""

LOCKS: dict[str, dict[str, Any]] = {}
FOLDERS_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
FOLDERS_TTL = 5.0
LIST_CACHE: dict[int, tuple[float, list[dict[str, Any]]]] = {}
LIST_TTL = 10.0


# ---- credentials ---------------------------------------------------------

def credentials() -> tuple[str, str]:
    user = db.kv_get("webdav_user") or config.WEBDAV_USER
    password = db.kv_get("webdav_pass", secret=True)
    if not password:
        password = secrets.token_urlsafe(18)
        db.kv_set("webdav_pass", password, secret=True)
        db.kv_set("webdav_user", user)
    return user, password


def _authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    username, _, password = decoded.partition(":")
    expected_user, expected_pass = credentials()
    return (hmac.compare_digest(username.encode(), expected_user.encode())
            and hmac.compare_digest(password.encode(), expected_pass.encode()))


def _unauthorized() -> Response:
    return Response(
        status_code=401,
        content="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="haodrive"'},
    )


# ---- helpers -------------------------------------------------------------

def _prefix(request: Request) -> str:
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")


def _href(request: Request, path: str, collection: bool = False) -> str:
    base = _prefix(request)
    encoded = "/".join(quote(part, safe="") for part in path.split("/"))
    if collection and not encoded.endswith("/"):
        encoded += "/"
    return f"{base}/dav{encoded}"


def _http_date(timestamp: int) -> str:
    return format_datetime(datetime.fromtimestamp(timestamp or 0, tz=timezone.utc), usegmt=True)


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp or 0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(path: str) -> list[str]:
    # FastAPI/Starlette already percent-decodes the path parameter.
    return [part for part in path.strip("/").split("/") if part]


async def _folders() -> list[dict[str, Any]]:
    global FOLDERS_CACHE
    from .main import backend
    stamp, cached = FOLDERS_CACHE
    if time.time() - stamp < FOLDERS_TTL:
        return cached
    folders = await backend.folders()
    FOLDERS_CACHE = (time.time(), folders)
    return folders


def invalidate(folder_id: int | None = None) -> None:
    global FOLDERS_CACHE
    FOLDERS_CACHE = (0.0, [])
    if folder_id is not None:
        LIST_CACHE.pop(folder_id, None)


async def _files(folder_id: int) -> list[dict[str, Any]]:
    from .main import backend
    cached = LIST_CACHE.get(folder_id)
    now = time.time()
    if cached and now - cached[0] < LIST_TTL:
        return cached[1]
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(20):  # at most 2000 entries per listing
        page = await backend.files(folder_id, offset=offset, limit=100)
        items.extend(page["items"])
        offset = page["next_offset"]
        if not offset:
            break
    # De-duplicate names so paths stay unique.
    seen: dict[str, int] = {}
    for item in items:
        name = item["name"]
        count = seen.get(name, 0)
        seen[name] = count + 1
        if count:
            stem, dot, ext = name.rpartition(".")
            item["name"] = f"{stem} ({count + 1}).{ext}" if dot else f"{name} ({count + 1})"
    LIST_CACHE[folder_id] = (time.time(), items)
    return items


def _folder_by_name(folders: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for folder in folders:
        if folder["name"] == name:
            return folder
    return None


async def _resolve(path: str) -> tuple[str, Any]:
    """Return ('root'|'folder'|'file'|'missing', payload)."""
    parts = _split(path)
    folders = await _folders()
    if not parts:
        return "root", folders
    folder = _folder_by_name(folders, parts[0])
    if folder is None:
        return "missing", None
    if len(parts) == 1:
        return "folder", folder
    if len(parts) > 2:
        return "missing", None
    for item in await _files(folder["id"]):
        if item["name"] == parts[1]:
            return "file", (folder, item)
    return "missing", None


async def _folder_files(folder_id: int) -> list[dict[str, Any]]:
    return await _files(folder_id)


def _lockdiscovery(resource_path: str) -> str:
    entry = LOCKS.get("/" + resource_path.strip("/"))
    if not entry or entry["expires"] < time.time():
        return "<D:lockdiscovery/>"
    return ("<D:lockdiscovery><D:activelock><D:locktype><D:write/></D:locktype>"
            "<D:lockscope><D:exclusive/></D:lockscope><D:depth>0</D:depth><D:timeout>Second-1800</D:timeout>"
            f"<D:locktoken><D:href>{escape(entry['token'])}</D:href></D:locktoken>"
            "</D:activelock></D:lockdiscovery>")


def _prop_response(href: str, display: str, is_collection: bool, size: int = 0,
                   modified: int = 0, content_type: str = "", etag: str = "",
                   lock_path: str = "") -> str:
    resource = "<D:resourcetype><D:collection/></D:resourcetype>" if is_collection else "<D:resourcetype/>"
    extra = ""
    if not is_collection:
        extra = (
            f"<D:getcontentlength>{size}</D:getcontentlength>"
            f"<D:getcontenttype>{escape(content_type or 'application/octet-stream')}</D:getcontenttype>"
        )
        if etag:
            extra += f"<D:getetag>{escape(etag)}</D:getetag>"
    return (
        "<D:response>"
        f"<D:href>{escape(href)}</D:href>"
        "<D:propstat><D:prop>"
        f"<D:displayname>{escape(display)}</D:displayname>"
        f"{resource}{extra}"
        f"<D:getlastmodified>{_http_date(modified)}</D:getlastmodified>"
        "<D:creationdate>" + _iso(modified) + "</D:creationdate>"
        "<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>"
        "<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>"
        + _lockdiscovery(lock_path)
        + "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response>"
    )


def _invalidate_media(folder_id: int, item: dict[str, Any] | None) -> None:
    if not item:
        return
    try:
        from .main import media_cache
        media_cache.invalidate_prefix(f"{folder_id}_{item.get('id')}_")
    except Exception:
        pass


def _lock_conflict(request: Request, path: str) -> Response | None:
    entry = LOCKS.get("/" + path.strip("/"))
    if not entry:
        return None
    if entry["expires"] < time.time():
        LOCKS.pop("/" + path.strip("/"), None)
        return None
    if entry["token"] not in request.headers.get("if", ""):
        return Response(status_code=423, content="Locked")
    return None


def _destination_path(request: Request) -> str:
    raw = request.headers.get("destination", "")
    if not raw:
        return ""
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    path = parsed.path or raw
    prefix = _prefix(request)
    if prefix and path.startswith(prefix):
        path = path[len(prefix):]
    if path.startswith("/dav"):
        path = path[4:]
    return unquote(path)


# ---- HTTP methods --------------------------------------------------------

@router.api_route("/{path:path}", methods=["OPTIONS"])
async def options(request: Request, path: str = "") -> Response:
    return Response(
        status_code=200,
        headers={
            "DAV": "1, 2",
            "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, MOVE, COPY, LOCK, UNLOCK",
            "MS-Author-Via": "DAV",
        },
    )


@router.api_route("/{path:path}", methods=["PROPFIND"])
async def propfind(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    depth = request.headers.get("depth", "1").lower()
    if depth == "infinity":
        return Response(status_code=403, content="propfind-finite-depth")
    kind, payload = await _resolve(path)
    if kind == "missing":
        return Response(status_code=404)

    body: list[str] = []
    if kind == "root":
        body.append(_prop_response(_href(request, "/", True), "/", True))
        if depth != "0":
            for folder in payload:
                body.append(_prop_response(_href(request, f"/{folder['name']}", True), folder["name"], True))
    elif kind == "folder":
        folder = payload
        body.append(_prop_response(_href(request, f"/{folder['name']}", True), folder["name"], True))
        if depth != "0":
            for item in await _folder_files(folder["id"]):
                body.append(
                    _prop_response(
                        _href(request, f"/{folder['name']}/{item['name']}"), item["name"], False,
                        size=item["size"], modified=item["date"], content_type=item.get("mime", ""),
                        etag=f'"{item["id"]}-{item["size"]}"',
                    )
                )
    else:
        folder, item = payload
        body.append(
            _prop_response(
                _href(request, f"/{folder['name']}/{item['name']}"), item["name"], False,
                size=item["size"], modified=item["date"], content_type=item.get("mime", ""),
                etag=f'"{item["id"]}-{item["size"]}"', lock_path=f"/{folder['name']}/{item['name']}",
            )
        )

    xml = PROPFIND_XML.format(body="".join(body))
    return Response(status_code=207, content=xml, media_type="application/xml; charset=utf-8")


@router.api_route("/{path:path}", methods=["PROPPATCH"])
async def proppatch(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    href = _href(request, "/" + path.strip("/"))
    xml = PROPFIND_XML.format(
        body=f"<D:response><D:href>{escape(href)}</D:href>"
             "<D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )
    return Response(status_code=207, content=xml, media_type="application/xml; charset=utf-8")


async def _stream_file(request: Request, folder: dict[str, Any], item: dict[str, Any], body: bool) -> Response:
    from .main import backend, counting_stream, media_cache

    size = int(item["size"])
    etag = f'"{item["id"]}-{size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    try:
        parsed = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, length = parsed if parsed else (0, size)
    content_type = item.get("mime") or "application/octet-stream"
    disposition = "inline"
    if content_type not in {
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
        "video/mp4", "video/webm", "audio/mpeg", "audio/ogg", "application/pdf",
    }:
        disposition = "attachment"
        content_type = "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Content-Disposition": disposition,
        "Last-Modified": _http_date(item["date"]),
        "ETag": etag,
    }
    if parsed:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
    headers["Content-Length"] = str(length)
    if not body:
        return Response(status_code=206 if parsed else 200, headers=headers)

    def fetcher(offset: int, limit: int):
        return backend.download_iter(folder["id"], item["id"], offset, limit)

    if backend.name == "telegram":
        stream = media_cache.stream(
            f"{folder['id']}_{item['id']}_{item.get('date', 0)}", size, start, length, fetcher
        )
    else:
        stream = backend.download_iter(folder["id"], item["id"], start, length)
    return StreamingResponse(counting_stream(stream), status_code=206 if parsed else 200,
                             headers=headers)


@router.api_route("/{path:path}", methods=["GET", "HEAD"])
async def get_file(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    kind, payload = await _resolve(path)
    if kind == "missing":
        return Response(status_code=404)
    if kind != "file":
        return Response(status_code=405, content="Collections cannot be downloaded")
    folder, item = payload
    return await _stream_file(request, folder, item, request.method != "HEAD")


@router.api_route("/{path:path}", methods=["PUT"])
async def put_file(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    from .main import backend

    pieces = _split(path)
    if len(pieces) != 2:
        return Response(status_code=409, content="PUT needs /<folder>/<file>")
    folders = await _folders()
    folder = _folder_by_name(folders, pieces[0])
    if folder is None:
        return Response(status_code=409, content="Folder does not exist")

    locked = _lock_conflict(request, path)
    if locked:
        return locked

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.MAX_UPLOAD_BYTES:
        return Response(status_code=413, content="File too large")

    existing = None
    for item in await _folder_files(folder["id"]):
        if item["name"] == pieces[1]:
            existing = item
            break

    hint = int(declared) if declared and declared.isdigit() else 0
    # Rolling spool: one Telegram-sized part at a time, never the whole file.
    # Upload first: a failed upload must never destroy the old file.
    info = await parts.upload_stream(backend, folder["id"], pieces[1],
                                     request.stream(), total_hint=hint)
    if existing and existing.get("id") != info.get("id"):
        await backend.delete_file(folder["id"], existing["id"])
    _invalidate_media(folder["id"], existing)
    _invalidate_media(folder["id"], info)
    invalidate(folder["id"])
    return Response(status_code=204 if existing else 201)


@router.api_route("/{path:path}", methods=["MKCOL"])
async def mkcol(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    from .main import backend

    parts = _split(path)
    if len(parts) != 1:
        return Response(status_code=409, content="Only top-level folders can be created")
    folders = await _folders()
    if _folder_by_name(folders, parts[0]):
        return Response(status_code=405)
    try:
        await backend.create_folder(parts[0])
    except BackendError as error:
        return Response(status_code=error.code, content=error.message)
    invalidate()
    return Response(status_code=201)


@router.api_route("/{path:path}", methods=["DELETE"])
async def delete(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    from .main import backend

    locked = _lock_conflict(request, path)
    if locked:
        return locked
    kind, payload = await _resolve(path)
    if kind == "missing":
        return Response(status_code=404)
    if kind == "root":
        return Response(status_code=403)
    if kind == "folder":
        await backend.delete_folder(payload["id"])
        invalidate(payload["id"])
    else:
        folder, item = payload
        await backend.delete_file(folder["id"], item["id"])
        LIST_CACHE.pop(folder["id"], None)
    return Response(status_code=204)


@router.api_route("/{path:path}", methods=["MOVE"])
async def move(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    from .main import backend

    locked = _lock_conflict(request, path)
    if locked:
        return locked

    destination = _destination_path(request)
    if not destination:
        return Response(status_code=400, content="Destination header required")
    src_kind, src = await _resolve(path)
    if src_kind != "file":
        return Response(status_code=501, content="Only files can be moved")
    src_folder, src_item = src
    dst_parts = _split(destination)
    if len(dst_parts) != 2:
        return Response(status_code=409)
    folders = await _folders()
    dst_folder = _folder_by_name(folders, dst_parts[0])
    if dst_folder is None:
        return Response(status_code=409)

    destination_item = None
    for item in await _folder_files(dst_folder["id"]):
        if item["name"] == dst_parts[1] and not (
            dst_folder["id"] == src_folder["id"] and item["id"] == src_item["id"]
        ):
            destination_item = item
            break
    overwrite = request.headers.get("overwrite", "T").upper() != "F"
    if destination_item and not overwrite:
        return Response(status_code=412)

    if dst_folder["id"] == src_folder["id"]:
        db.set_display_name(src_folder["id"], src_item["id"], dst_parts[1])
    else:
        new_id = await backend.move_file(src_folder["id"], src_item["id"], dst_folder["id"])
        if new_id and dst_parts[1] != src_item["name"]:
            db.set_display_name(dst_folder["id"], new_id, dst_parts[1])
    if destination_item:
        # Only after the move succeeded: a failed move must not lose the target.
        await backend.delete_file(dst_folder["id"], destination_item["id"])
        _invalidate_media(dst_folder["id"], destination_item)
    _invalidate_media(src_folder["id"], src_item)
    invalidate(src_folder["id"])
    invalidate(dst_folder["id"])
    return Response(status_code=204 if destination_item else 201)


@router.api_route("/{path:path}", methods=["COPY"])
async def copy(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    from .main import backend

    destination = _destination_path(request)
    src_kind, src = await _resolve(path)
    if src_kind != "file":
        return Response(status_code=501, content="Only files can be copied")
    src_folder, src_item = src
    dst_parts = _split(destination)
    if len(dst_parts) != 2:
        return Response(status_code=409)
    folders = await _folders()
    dst_folder = _folder_by_name(folders, dst_parts[0])
    if dst_folder is None:
        return Response(status_code=409)
    destination_item = None
    for item in await _folder_files(dst_folder["id"]):
        if item["name"] == dst_parts[1]:
            destination_item = item
            break
    overwrite = request.headers.get("overwrite", "T").upper() != "F"
    if destination_item and not overwrite:
        return Response(status_code=412)
    if destination_item and dst_folder["id"] == src_folder["id"] and destination_item["id"] == src_item["id"]:
        return Response(status_code=204)  # copying onto itself is a no-op
    new_id = await backend.copy_file(src_folder["id"], src_item["id"], dst_folder["id"])
    if destination_item:
        await backend.delete_file(dst_folder["id"], destination_item["id"])
        _invalidate_media(dst_folder["id"], destination_item)
    if new_id and dst_parts[1] != src_item["name"]:
        db.set_display_name(dst_folder["id"], new_id, dst_parts[1])
    invalidate(dst_folder["id"])
    return Response(status_code=204 if destination_item else 201)


@router.api_route("/{path:path}", methods=["LOCK"])
async def lock(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    key = "/" + path.strip("/")
    existing_lock = LOCKS.get(key)
    if existing_lock and existing_lock["expires"] > time.time():
        if existing_lock["token"] not in request.headers.get("if", ""):
            return Response(status_code=423, content="Locked")
        existing_lock["expires"] = time.time() + 1800
        token = existing_lock["token"]
    else:
        token = f"opaquelocktoken:{uuid.uuid4()}"
        LOCKS[key] = {"token": token, "expires": time.time() + 1800}
    timeout = request.headers.get("timeout", "Second-1800")
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
        "<D:locktype><D:write/></D:locktype>"
        "<D:lockscope><D:exclusive/></D:lockscope>"
        f"<D:depth>infinity</D:depth><D:timeout>{escape(timeout)}</D:timeout>"
        f"<D:locktoken><D:href>{escape(token)}</D:href></D:locktoken>"
        "</D:activelock></D:lockdiscovery></D:prop>"
    )
    return Response(status_code=200, content=xml, media_type="application/xml; charset=utf-8",
                    headers={"Lock-Token": f"<{token}>"})


@router.api_route("/{path:path}", methods=["UNLOCK"])
async def unlock(request: Request, path: str = "") -> Response:
    if not _authorized(request):
        return _unauthorized()
    token = request.headers.get("lock-token", "").strip("<>")
    key = "/" + path.strip("/")
    entry = LOCKS.get(key)
    if entry and entry["token"] == token:
        LOCKS.pop(key, None)
        return Response(status_code=204)
    return Response(status_code=409)
