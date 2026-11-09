"""FastAPI application for haodrive (VPS Telegram cloud drive)."""
from __future__ import annotations

import asyncio
import datetime
import logging
import mimetypes
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import platform

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from . import config, db, i18n, parts, security, settings, traffic, webdav
from .demo_backend import DemoBackend
from .http_range import RangeNotSatisfiable, if_range_matches, parse_range
from .media_cache import MediaCache
from .stream_upload import STREAM_MIN_BYTES, STREAM_PART, StreamError, StreamUpload
from .tg_backend import BackendError, TelegramBackend, UploadJob, friendly_error

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("haodrive")

app = FastAPI(title="haodrive", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(webdav.router, prefix="/dav")

backend = DemoBackend() if config.DEMO_MODE else TelegramBackend()
APP_VERSION = "11.2.0"
media_cache = MediaCache(config.CACHE_DIR / "media", int(config.MEDIA_CACHE_GB * 1024**3))
STARTED_AT = time.time()


def _apply_cache_size(gb: float) -> None:
    """Resize the media cache in place.

    Rebuilding MediaCache would wipe the on-disk blocks (its constructor
    clears the directory), which would yank the file out from under anyone
    currently streaming. Updating the ceiling and evicting is enough.
    """
    media_cache.set_capacity(int(float(gb) * 1024**3))


settings.on_change("media_cache_gb", _apply_cache_size)
SHARE_COOKIE = "hd_share"
UPLOAD_SESSIONS: dict[str, dict[str, Any]] = {}
UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}
MAX_UPLOAD_SESSIONS = 32
SHARE_SAFE_INLINE = {"image", "video", "audio", "pdf"}


# ---- lifecycle -----------------------------------------------------------

def _drop_stream_session(upload_id: str) -> None:
    UPLOAD_SESSIONS.pop(upload_id, None)
    UPLOAD_LOCKS.pop(upload_id, None)


def _restore_stream(row: dict[str, Any], now: float) -> None:
    """Rebuild a live-to-Telegram session after a restart.

    The bytes between the last confirmed SaveBigFilePart and the browser's old
    offset are gone, so the session restarts at that part boundary and the
    browser re-sends from there.  Telegram keeps the already-accepted parts
    for a while, so the work done before the restart is not lost.
    """
    upload_id = row["id"]
    group_key = row.get("group_key")
    part_index = row.get("part_index")
    if now - row["created"] > 86400 or not row.get("file_id"):
        db.upload_delete(upload_id)
        if group_key is not None and part_index is not None:
            db.part_group_set_state(group_key, part_index, "pending")
        return
    group = db.part_group_get(group_key) if group_key is not None else None
    if group_key is not None:
        part = next((p for p in db.part_group_parts(group_key)
                     if p["part_index"] == part_index), None) if group else None
        if not group or not part or part["state"] == "done":
            db.upload_delete(upload_id)
            return
    resume_parts = int(row.get("confirmed") or 0) // STREAM_PART
    job = UploadJob(id=upload_id, name=row["name"], folder_id=int(row["folder_id"]),
                    state="queued", sent=min(int(row["size"]), resume_parts * STREAM_PART),
                    total=int(row["size"]), group_key=group_key, part_index=part_index,
                    part_count=int(group["part_count"]) if group else 1)
    upload = StreamUpload(
        backend, job, upload_id=upload_id, folder_id=int(row["folder_id"]),
        name=row["name"], size=int(row["size"]), file_id=int(row["file_id"]),
        group_key=group_key, part_index=part_index,
        part_count=int(group["part_count"]) if group else 1,
        resume_parts=resume_parts, on_finished=_drop_stream_session)
    backend.jobs[upload_id] = job
    UPLOAD_SESSIONS[upload_id] = {
        "folder_id": row["folder_id"], "name": row["name"], "size": row["size"],
        "path": None, "received": upload.fed, "created": row["created"],
        "group_key": group_key, "part_index": part_index,
        "part_count": int(group["part_count"]) if group else 1,
        "stream": True, "upload": upload,
    }
    UPLOAD_LOCKS[upload_id] = asyncio.Lock()
    logger.info("restored streaming upload %s at %s bytes", row["name"], upload.fed)


def _restore_uploads() -> None:
    """Reload resumable sessions from SQLite and drop stale temp files."""
    now = time.time()
    known: set[str] = set()
    try:
        rows = db.upload_load_all()
    except Exception:
        rows = []
    for row in rows:
        upload_id = row["id"]
        if row.get("stream"):
            _restore_stream(row, now)
            known.add(upload_id)
            continue
        path = config.TMP_DIR / upload_id
        if not path.exists():
            db.upload_delete(upload_id)
            continue
        if now - row["created"] > 86400:
            path.unlink(missing_ok=True)
            db.upload_delete(upload_id)
            continue
        actual = path.stat().st_size
        if actual > int(row["size"]):
            path.unlink(missing_ok=True)
            db.upload_delete(upload_id)
            continue
        received = min(int(row["received"]), actual)
        group_key = row.get("group_key")
        part_index = row.get("part_index")
        if group_key is not None:
            group = db.part_group_get(group_key)
            part = next((p for p in db.part_group_parts(group_key)
                         if p["part_index"] == part_index), None) if group else None
            if not group or not part or part["state"] == "done":
                path.unlink(missing_ok=True)
                db.upload_delete(upload_id)
                continue
        UPLOAD_SESSIONS[upload_id] = {
            "folder_id": row["folder_id"], "name": row["name"], "size": row["size"],
            "path": path, "received": received, "created": row["created"],
            "group_key": group_key, "part_index": part_index,
        }
        UPLOAD_LOCKS[upload_id] = asyncio.Lock()
        known.add(upload_id)
    for child in config.TMP_DIR.iterdir():
        try:
            if child.is_file() and child.name not in known and now - child.stat().st_mtime > 86400:
                child.unlink()
        except OSError:
            pass


async def _restore_jobs() -> None:
    """Re-queue unfinished jobs whose temp file survived, fail the rest."""
    if not config.DEMO_MODE and getattr(backend, "state", "") != "ready":
        return  # nothing can be sent until Telegram is connected
    for row in db.job_unfinished():
        path = config.TMP_DIR / row["id"]
        if path.exists():
            job = UploadJob(id=row["id"], name=row["name"], folder_id=row["folder_id"],
                            total=row["total"] or path.stat().st_size,
                            group_key=row.get("group_key"),
                            part_index=row.get("part_index"),
                            part_count=row.get("part_count"))
            await backend.enqueue_upload(job)
            logger.info("requeued job %s (%s)", job.id, job.name)
        else:
            job = UploadJob(id=row["id"], name=row["name"], folder_id=row["folder_id"],
                            total=row["total"], state="error",
                            error="服务重启，临时文件已清理，请重新上传",
                            group_key=row.get("group_key"),
                            part_index=row.get("part_index"),
                            part_count=row.get("part_count"))
            backend.jobs.setdefault(job.id, job)
            db.job_save(job)


TRAFFIC_TASK: asyncio.Task | None = None


async def _traffic_loop() -> None:
    while True:
        await asyncio.sleep(15)
        await asyncio.to_thread(traffic.flush)


def _finalize_pending_groups() -> None:
    """Finish groups whose parts all arrived before the last restart."""
    for group in db.part_groups_unfinished():
        try:
            registered = db.part_group_finalize(group["group_key"])
        except Exception:
            logger.exception("could not finalize upload group %s", group["group_key"])
            continue
        if registered:
            db.storage_add(registered["folder_id"], 1, registered["size"])
            logger.info("finalized upload group %s (%s parts)", group["name"], registered["parts"])


async def _refresh_storage() -> None:
    """Recompute per-folder storage from the backend and cache it in SQLite."""
    try:
        folders = await backend.folders()
    except Exception:
        logger.exception("storage refresh: could not list folders")
        return
    rows: list[tuple[int, int, int]] = []
    for folder in folders:
        count = 0
        size = 0
        offset = 0
        for _ in range(10000):
            try:
                data = await backend.files(folder["id"], offset=offset, limit=200)
            except Exception:
                logger.exception("storage refresh failed for folder %s", folder["id"])
                break
            for item in data.get("items", []):
                count += 1
                size += int(item.get("size") or 0)
            offset = int(data.get("next_offset") or 0)
            if not offset:
                break
        rows.append((int(folder["id"]), count, size))
    db.storage_replace_all(rows)
    logger.info("storage refreshed: %s folders", len(rows))


@app.on_event("startup")
async def _startup() -> None:
    global TRAFFIC_TASK
    db.init_db()
    settings.apply_all()
    _restore_uploads()
    db.job_prune()
    await backend.start()
    await _restore_jobs()
    _finalize_pending_groups()
    TRAFFIC_TASK = asyncio.create_task(_traffic_loop())
    if config.DEMO_MODE:
        await _refresh_storage()
    elif getattr(backend, "state", "") == "ready":
        asyncio.create_task(_refresh_storage())
    logger.info("haodrive started (demo=%s, cache=%s)", config.DEMO_MODE, media_cache.stats())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if TRAFFIC_TASK is not None:
        TRAFFIC_TASK.cancel()
        try:
            await TRAFFIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    await asyncio.to_thread(traffic.flush)
    client = getattr(backend, "client", None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---- middleware ----------------------------------------------------------

PUBLIC_API = {"/api/login", "/api/session"}
PUBLIC_PATHS = {"/", "/healthz", "/readyz", "/sw.js"}


def _public(path: str) -> bool:
    return (path in PUBLIC_API or path in PUBLIC_PATHS
            or path.startswith("/static/") or path.startswith("/s/") or path.startswith("/dav"))


@app.middleware("http")
async def request_pipeline(request: Request, call_next):
    started = time.time()
    request_id = uuid.uuid4().hex[:12]
    path = request.url.path
    # Authenticate before the framework reads a multipart/JSON body, so an
    # anonymous request cannot make the server buffer a 2 GB upload first.
    if path.startswith("/api/") and not _public(path):
        if not security.verify_session(request.cookies.get(security.COOKIE_NAME)):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    if path == "/api/login":
        # Throttle before FastAPI reads the JSON body, and cap its size.
        length = request.headers.get("content-length", "0")
        if length.isdigit() and int(length) > 4096:
            return JSONResponse({"detail": "请求体过大"}, status_code=413)
        if not security.login_limiter.allow(security.client_ip(request)):
            return JSONResponse({"detail": "尝试次数太多，等五分钟再试"}, status_code=429)
    response = await call_next(request)
    elapsed = (time.time() - started) * 1000
    log_path = "/s/***" if path.startswith("/s/") else path
    logger.info("%s %s -> %s %.0fms req=%s", request.method, log_path,
                getattr(response, "status_code", "?"), elapsed, request_id)
    response.headers.setdefault("X-Request-Id", request_id)
    if path.startswith("/api/") or path.startswith("/s"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/dav"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; media-src 'self'; frame-ancestors 'none'",
        )
    if path.startswith("/s/") or path == "/s":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
    elif request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
            "frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
    return response


@app.exception_handler(BackendError)
async def _backend_error(_: Request, error: BackendError) -> JSONResponse:
    return JSONResponse({"detail": error.message}, status_code=error.code)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, error: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": error.detail}, status_code=error.status_code)


def guard(request: Request, mutation: bool = False) -> None:
    security.require_admin(request)
    if mutation:
        security.require_mutation(request)


# ---- pages ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> Response:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/sw.js")
async def service_worker(request: Request) -> Response:
    response = FileResponse(Path(__file__).parent / "static" / "sw.js",
                            media_type="application/javascript")
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    response.headers["Service-Worker-Allowed"] = (prefix or "") + "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "1"}


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    checks: dict[str, Any] = {}
    status = 200
    try:
        db.kv_get("session_epoch", "0")
        checks["db"] = "ok"
    except Exception as error:
        checks["db"] = f"error: {error}"
        status = 503
    try:
        free = shutil.disk_usage(config.DATA_DIR).free
        checks["disk_free_mb"] = free // (1024 * 1024)
        if free < 200 * 1024 * 1024:
            status = 503
    except Exception as error:
        checks["disk_free_mb"] = f"error: {error}"
        status = 503
    tg_state = getattr(backend, "state", "demo" if config.DEMO_MODE else "unknown")
    checks["telegram"] = tg_state
    if not config.DEMO_MODE and tg_state != "ready":
        status = 503
    if not security.verify_session(request.cookies.get(security.COOKIE_NAME)):
        return JSONResponse({"status": "ok" if status == 200 else "degraded"}, status_code=status)
    checks["cache"] = media_cache.stats()
    return JSONResponse({"status": "ok" if status == 200 else "degraded", **checks}, status_code=status)


# ---- session / auth ------------------------------------------------------

@app.get("/api/session")
async def session_info(request: Request) -> dict[str, Any]:
    authed = security.verify_session(request.cookies.get(security.COOKIE_NAME))
    info: dict[str, Any] = {
        "authenticated": authed,
        "demo": config.DEMO_MODE,
        "site_name": config.SITE_NAME,
        "version": APP_VERSION,
    }
    if authed:
        info["telegram"] = await backend.status()
    return info


@app.post("/api/login")
async def login(request: Request, payload: dict[str, str]) -> JSONResponse:
    ip = security.client_ip(request)
    password = payload.get("password", "")
    if not isinstance(password, str):
        raise HTTPException(400, "密码格式不对")
    ok = await asyncio.to_thread(security.check_password, password)
    if not ok:
        security.login_limiter.record(ip)
        logger.warning("login failed from %s", ip)
        await asyncio.sleep(min(0.4 + 0.2 * security.login_limiter.failures(ip), 2.0))
        raise HTTPException(401, "密码错误")
    security.login_limiter.clear(ip)
    logger.info("login ok from %s", ip)
    response = JSONResponse({"ok": True})
    security.set_session_cookie(response, request, security.issue_session())
    return response


@app.post("/api/logout")
async def logout(request: Request) -> Response:
    guard(request, mutation=True)
    security.bump_session_epoch()
    response = JSONResponse({"ok": True})
    security.clear_session_cookie(response)
    return response


# ---- telegram login ------------------------------------------------------

@app.get("/api/tg/status")
async def tg_status(request: Request) -> dict[str, Any]:
    guard(request)
    return await backend.status()


@app.post("/api/tg/login/start")
async def tg_login_start(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    return await backend.begin_login(
        int(payload.get("api_id") or 0),
        str(payload.get("api_hash") or ""),
        str(payload.get("phone") or ""),
    )


@app.post("/api/tg/login/code")
async def tg_login_code(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    return await backend.submit_code(str(payload.get("code") or ""))


@app.post("/api/tg/login/password")
async def tg_login_password(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    return await backend.submit_password(str(payload.get("password") or ""))


@app.post("/api/tg/logout")
async def tg_logout(request: Request) -> dict[str, Any]:
    guard(request, mutation=True)
    await backend.logout()
    return await backend.status()


# ---- folders -------------------------------------------------------------

@app.get("/api/folders")
async def folders(request: Request) -> dict[str, Any]:
    guard(request)
    return {"folders": await backend.folders()}


@app.post("/api/folders")
async def create_folder(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    return await backend.create_folder(str(payload.get("name") or ""))


@app.delete("/api/folders/{folder_id}")
async def delete_folder(request: Request, folder_id: int) -> dict[str, Any]:
    guard(request, mutation=True)
    await backend.delete_folder(folder_id)
    return {"ok": True}


# ---- files ---------------------------------------------------------------

@app.get("/api/folders/{folder_id}/files")
async def list_files(request: Request, folder_id: int, offset: int = 0,
                     limit: int = 60, q: str | None = None) -> dict[str, Any]:
    guard(request)
    return await backend.files(folder_id, offset=offset, limit=limit, search=q or None)


@app.get("/api/folders/{folder_id}/files/{message_id}")
async def file_info(request: Request, folder_id: int, message_id: int) -> dict[str, Any]:
    guard(request)
    return await backend.file_info(folder_id, message_id)


@app.delete("/api/folders/{folder_id}/files/{message_id}")
async def delete_file(request: Request, folder_id: int, message_id: int) -> dict[str, Any]:
    guard(request, mutation=True)
    await backend.delete_file(folder_id, message_id)
    media_cache.invalidate_prefix(f"{folder_id}_{message_id}_")
    return {"ok": True}


@app.post("/api/folders/{folder_id}/files/{message_id}/move")
async def move_file(request: Request, folder_id: int, message_id: int,
                    payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    target = int(payload.get("to") or 0)
    if target == folder_id:
        raise HTTPException(400, "目标文件夹相同")
    await backend.move_file(folder_id, message_id, target)
    return {"ok": True}


@app.post("/api/folders/{folder_id}/files/{message_id}/rename")
async def rename_file(request: Request, folder_id: int, message_id: int,
                      payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "文件名不能为空")
    return await backend.rename_file(folder_id, message_id, name)


# ---- upload (multipart, kept for scripted clients) ----------------------

@app.post("/api/folders/{folder_id}/upload")
async def upload(request: Request, folder_id: int,
                 files: list[UploadFile] = File(...)) -> dict[str, Any]:
    guard(request, mutation=True)
    if len(files) > 20:
        raise HTTPException(413, "一次最多上传 20 个文件")
    if shutil.disk_usage(config.TMP_DIR).free < 512 * 1024 * 1024:
        raise HTTPException(507, "服务器磁盘空间不足")
    budget_left = config.TMP_BUDGET_BYTES - parts.tmp_usage() - 64 * 1024 * 1024
    if budget_left <= 0:
        raise HTTPException(507, "服务器临时空间不足，请等上一个上传完成")
    jobs: list[dict[str, Any]] = []
    for upload_file in files:
        job_id = uuid.uuid4().hex
        raw_name = os.path.basename(upload_file.filename or "file")
        safe_name = "".join(ch for ch in raw_name if ch.isprintable() and ch not in "/\\").strip() or "file"
        target = config.TMP_DIR / job_id
        total = 0
        try:
            with target.open("wb") as handle:
                os.chmod(target, 0o600)
                while True:
                    chunk = await upload_file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > config.MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"文件超过 {config.MAX_UPLOAD_BYTES // (1024 ** 3)}GB 上限")
                    if total > budget_left:
                        raise HTTPException(413, "超过服务器 2GB 临时空间，请用网页端分片上传")
                    traffic.add(up=len(chunk))
                    await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await upload_file.close()
        budget_left -= total
        job = UploadJob(id=job_id, name=safe_name, folder_id=folder_id, total=total)
        db.job_save(job)
        await backend.enqueue_upload(job)
        jobs.append({"id": job_id, "name": safe_name, "total": total})
    return {"jobs": jobs}



# ---- client-side part upload (browser slices the file) ------------------

def _safe_name(raw: Any) -> str:
    text = os.path.basename(str(raw or "file"))
    return "".join(ch for ch in text if ch.isprintable() and ch not in "/\\").strip() or "file"


@app.get("/api/parts/limits")
async def part_limits(request: Request) -> dict[str, Any]:
    guard(request)
    return {"part_bytes": config.TELEGRAM_PART_BYTES,
            "tmp_budget": config.TMP_BUDGET_BYTES,
            "max_bytes": config.MAX_UPLOAD_BYTES,
            "chunk_size": config.UPLOAD_CHUNK}


@app.post("/api/parts")
async def create_part(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    _purge_uploads()
    if len(UPLOAD_SESSIONS) >= MAX_UPLOAD_SESSIONS:
        raise HTTPException(429, "同时进行的上传太多，先完成或取消一些")
    folder_id = int(payload.get("folder_id") or 0)
    name = _safe_name(payload.get("name"))
    size = int(payload.get("size") or 0)
    part_bytes = int(payload.get("part_bytes") or config.TELEGRAM_PART_BYTES)
    part_bytes = max(1, min(part_bytes, config.TELEGRAM_PART_BYTES))
    group_key = str(payload.get("group_key") or "")
    if not parts.group_key_ok(group_key):
        raise HTTPException(400, "group_key 不合法")
    if size < 0:
        raise HTTPException(400, "size 不合法")
    if size > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {config.MAX_UPLOAD_BYTES // (1024 ** 3)}GB 上限")
    expected = parts.part_sizes(size, part_bytes)
    part_count = int(payload.get("part_count") or len(expected))
    part_index = int(payload.get("part_index") or 0)
    if part_count != len(expected):
        raise HTTPException(409, "分片数量和文件大小对不上")
    if not 0 <= part_index < part_count:
        raise HTTPException(400, "part_index 越界")
    part_size = expected[part_index]
    mime, kind = parts.mime_kind(name)

    group = db.part_group_get(group_key)
    if group:
        if (group["folder_id"] != folder_id or group["name"] != name
                or group["size"] != size or group["part_bytes"] != part_bytes
                or group["part_count"] != part_count):
            raise HTTPException(409, "上传分组参数不一致")
    else:
        group = db.part_group_create(group_key, folder_id, name, size, part_bytes,
                                     part_count, mime, kind, expected)
        logger.info("part group created: %s (%s parts, %s bytes)", name, part_count, size)

    row = next((p for p in db.part_group_parts(group_key)
                if p["part_index"] == part_index), None)
    if row and row["state"] == "done" and row["message_id"]:
        return {"state": "done", "part_index": part_index, "part_count": part_count,
                "message_id": row["message_id"], "size": part_size,
                "offset": part_size}

    for uid, session in list(UPLOAD_SESSIONS.items()):
        if session.get("group_key") != group_key or session.get("part_index") != part_index:
            continue
        upload = session.get("upload")
        if upload is not None and upload.state in ("error", "cancelled"):
            # The pump is dead; resume from the last confirmed part boundary
            # instead of handing back a session that can never accept data.
            _drop_stream_session(uid)
            db.upload_delete(uid)
            continue
        return {"id": uid, "offset": session["received"], "size": part_size,
                "chunk_size": config.UPLOAD_CHUNK, "state": "uploading",
                "part_index": part_index, "part_count": part_count,
                "sent": upload.confirmed_bytes if upload is not None else 0,
                "stream": bool(session.get("stream"))}

    stale = db.part_group_upload_session(group_key, part_index)
    resume_row = None
    if stale:
        row = db.upload_get(stale["id"])
        if (row and row.get("stream") and row.get("file_id")
                and time.time() - int(row["created"]) < 86400):
            # Keep Telegram's file_id and its confirmed parts: the retry only
            # has to send the tail, not the whole part again.
            resume_row = row
        db.upload_delete(stale["id"])
    in_flight = db.job_find_part(group_key, part_index)
    if in_flight:
        raise HTTPException(409, "这一片正在发往 Telegram，等它完成")

    if getattr(backend, "supports_streaming", False) and part_size > STREAM_MIN_BYTES:
        return _create_stream_part(folder_id, name, part_size, group_key,
                                   part_index, part_count, resume_row)

    used = parts.tmp_usage()
    if used + part_size > config.TMP_BUDGET_BYTES:
        raise HTTPException(507, "服务器临时空间不足（2GB 上限），等上一片发完再试")
    free = shutil.disk_usage(config.TMP_DIR).free
    if free < 512 * 1024 * 1024 or free < part_size * 1.1:
        raise HTTPException(507, "服务器磁盘空间不足")

    uid = uuid.uuid4().hex
    path = config.TMP_DIR / uid
    path.touch()
    os.chmod(path, 0o600)
    session = {"folder_id": folder_id, "name": name, "size": part_size,
               "path": path, "received": 0, "created": time.time(),
               "group_key": group_key, "part_index": part_index,
               "part_count": part_count}
    UPLOAD_SESSIONS[uid] = session
    UPLOAD_LOCKS[uid] = asyncio.Lock()
    db.upload_save(uid, folder_id, name, part_size, 0, int(session["created"]),
                   group_key, part_index)
    db.part_group_set_state(group_key, part_index, "uploading")
    return {"id": uid, "offset": 0, "size": part_size, "chunk_size": config.UPLOAD_CHUNK,
            "state": "uploading", "part_index": part_index, "part_count": part_count}


def _create_stream_part(folder_id: int, name: str, part_size: int, group_key: str,
                        part_index: int, part_count: int,
                        resume_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a part that goes to Telegram as the browser is still sending it."""
    uid = uuid.uuid4().hex
    if resume_row is not None:
        file_id = int(resume_row["file_id"])
        resume_parts = int(resume_row.get("confirmed") or 0) // STREAM_PART
    else:
        file_id = random.getrandbits(63) or 1
        resume_parts = 0
    resume_bytes = min(part_size, resume_parts * STREAM_PART)
    job = UploadJob(id=uid, name=name, folder_id=folder_id, state="queued",
                    sent=resume_bytes, total=part_size, group_key=group_key,
                    part_index=part_index, part_count=part_count)
    upload = StreamUpload(
        backend, job, upload_id=uid, folder_id=folder_id, name=name, size=part_size,
        file_id=file_id, group_key=group_key, part_index=part_index,
        part_count=part_count, resume_parts=resume_parts,
        on_finished=_drop_stream_session)
    backend.jobs[uid] = job
    session = {"folder_id": folder_id, "name": name, "size": part_size, "path": None,
               "received": upload.fed, "created": time.time(), "group_key": group_key,
               "part_index": part_index, "part_count": part_count,
               "stream": True, "upload": upload}
    UPLOAD_SESSIONS[uid] = session
    UPLOAD_LOCKS[uid] = asyncio.Lock()
    # No temp file: only the file_id has to survive a restart, plus how many
    # whole parts Telegram has confirmed.
    db.upload_save(uid, folder_id, name, part_size, upload.fed, int(session["created"]),
                   group_key, part_index, stream=1, file_id=file_id,
                   confirmed=resume_bytes)
    db.part_group_set_state(group_key, part_index, "uploading")
    logger.info("stream part started: %s part %s/%s (%s bytes, resuming at %s)",
                name, part_index + 1, part_count, part_size, resume_bytes)
    return {"id": uid, "offset": upload.fed, "size": part_size,
            "chunk_size": config.UPLOAD_CHUNK, "state": "uploading",
            "part_index": part_index, "part_count": part_count,
            "stream": True, "sent": resume_bytes}


@app.delete("/api/parts/{group_key}")
async def cancel_part_group(request: Request, group_key: str) -> dict[str, Any]:
    guard(request, mutation=True)
    group = db.part_group_get(group_key)
    if not group:
        return {"ok": True}
    for uid, session in list(UPLOAD_SESSIONS.items()):
        if session.get("group_key") != group_key:
            continue
        upload = session.get("upload")
        if upload is not None:
            await upload.cancel()
        UPLOAD_SESSIONS.pop(uid, None)
        UPLOAD_LOCKS.pop(uid, None)
        try:
            if session.get("path") is not None:
                session["path"].unlink()
        except OSError:
            pass
        db.upload_delete(uid)
    sent = [int(p["message_id"]) for p in db.part_group_parts(group_key)
            if p["state"] == "done" and p["message_id"]]
    try:
        await backend.delete_messages_only(group["folder_id"], sent)
    except Exception:
        logger.exception("could not delete cancelled part messages")
    db.part_group_delete(group_key)
    db.upload_delete_group(group_key)
    return {"ok": True}


@app.get("/api/parts/{group_key}")
async def part_group_status(request: Request, group_key: str) -> dict[str, Any]:
    guard(request)
    group = db.part_group_get(group_key)
    if not group:
        raise HTTPException(404, "上传分组不存在")
    sessions = {s.get("part_index"): uid for uid, s in UPLOAD_SESSIONS.items()
                if s.get("group_key") == group_key}
    out: list[dict[str, Any]] = []
    for row in db.part_group_parts(group_key):
        item: dict[str, Any] = {"index": row["part_index"], "size": row["part_size"],
                                "state": row["state"], "message_id": row["message_id"]}
        uid = sessions.get(row["part_index"])
        if uid:
            item.update({"upload_id": uid, "offset": UPLOAD_SESSIONS[uid]["received"]})
        out.append(item)
    return {"group_key": group_key, "name": group["name"], "size": group["size"],
            "part_bytes": group["part_bytes"], "part_count": group["part_count"],
            "parts": out}


# ---- resumable (chunked) upload -----------------------------------------

def _purge_uploads() -> None:
    cutoff = time.time() - 86400
    for uid, session in list(UPLOAD_SESSIONS.items()):
        if session["created"] < cutoff:
            upload = session.get("upload")
            if upload is not None:
                asyncio.create_task(upload.cancel())
            try:
                if session.get("path") is not None:
                    session["path"].unlink()
            except OSError:
                pass
            db.upload_delete(uid)
            session_part = session
            UPLOAD_SESSIONS.pop(uid, None)
            UPLOAD_LOCKS.pop(uid, None)
            if session_part.get("group_key") is not None and session_part.get("part_index") is not None:
                db.part_group_set_state(session_part["group_key"],
                                        session_part["part_index"], "pending")


@app.post("/api/uploads")
async def create_upload(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    _purge_uploads()
    if len(UPLOAD_SESSIONS) >= MAX_UPLOAD_SESSIONS:
        raise HTTPException(429, "同时进行的上传太多，先完成或取消一些")
    folder_id = int(payload.get("folder_id") or 0)
    raw_name = os.path.basename(str(payload.get("name") or "file"))
    name = "".join(ch for ch in raw_name if ch.isprintable() and ch not in "/\\").strip() or "file"
    size = int(payload.get("size") or 0)
    if size < 0:
        raise HTTPException(400, "size 不合法")
    if size > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {config.MAX_UPLOAD_BYTES // (1024 ** 3)}GB 上限")
    if size > config.TMP_BUDGET_BYTES:
        raise HTTPException(413, "超过 2GB 临时空间上限，请使用网页端分片上传")
    if parts.tmp_usage() + size > config.TMP_BUDGET_BYTES:
        raise HTTPException(507, "服务器临时空间不足，请等上一个上传完成")
    free = shutil.disk_usage(config.TMP_DIR).free
    if free < 512 * 1024 * 1024 or free < size * 1.1:
        raise HTTPException(507, "服务器磁盘空间不足")
    uid = uuid.uuid4().hex
    path = config.TMP_DIR / uid
    path.touch()
    os.chmod(path, 0o600)
    session = {"folder_id": folder_id, "name": name, "size": size,
               "path": path, "received": 0, "created": time.time()}
    UPLOAD_SESSIONS[uid] = session
    UPLOAD_LOCKS[uid] = asyncio.Lock()
    db.upload_save(uid, folder_id, name, size, 0, int(session["created"]))
    return {"id": uid, "offset": 0, "chunk_size": config.UPLOAD_CHUNK}


@app.get("/api/uploads/{upload_id}")
async def upload_status(request: Request, upload_id: str) -> dict[str, Any]:
    guard(request)
    session = UPLOAD_SESSIONS.get(upload_id)
    if not session:
        raise HTTPException(404, "上传会话不存在")
    return {"id": upload_id, "offset": session["received"], "size": session["size"]}


async def _feed_stream(upload: StreamUpload, session: dict[str, Any], upload_id: str,
                       request: Request) -> dict[str, Any]:
    """Pour an HTTP body into a live Telegram upload.

    Waiting on a full buffer is the backpressure: the socket stops being read
    while Telegram lags, so the browser is throttled to Telegram's pace instead
    of filling the VPS disk.  A dropped connection is not fatal -- the session
    stays and the browser resumes from the last confirmed part.
    """
    written = 0
    failure: StreamError | None = None
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            await upload.feed(chunk)
            written += len(chunk)
            traffic.add(up=len(chunk))
    except ClientDisconnect:
        pass
    except StreamError as error:
        failure = error
    finally:
        session["received"] = upload.fed
        if written:
            db.upload_save(upload_id, session["folder_id"], session["name"],
                           session["size"], session["received"], int(session["created"]),
                           session.get("group_key"), session.get("part_index"),
                           stream=1, file_id=upload.file_id,
                           confirmed=upload.confirmed_bytes)
    if failure is not None:
        raise HTTPException(failure.code, failure.message)
    if upload.error is not None:
        raise HTTPException(upload.error.code, upload.error.message)
    return {"offset": upload.fed, "size": session["size"],
            "sent": upload.confirmed_bytes, "stream": True}


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(request: Request, upload_id: str, offset: int = 0) -> dict[str, Any]:
    guard(request, mutation=True)
    session = UPLOAD_SESSIONS.get(upload_id)
    if not session:
        raise HTTPException(404, "上传会话不存在")
    lock = UPLOAD_LOCKS.setdefault(upload_id, asyncio.Lock())
    async with lock:
        if offset != session["received"]:
            return JSONResponse({"detail": "offset mismatch", "offset": session["received"]},
                                status_code=409)
        upload = session.get("upload")
        if upload is not None:
            return await _feed_stream(upload, session, upload_id, request)
        written = 0
        with session["path"].open("r+b") as handle:
            handle.seek(offset)
            async for chunk in request.stream():
                if not chunk:
                    continue
                if session["received"] + written + len(chunk) > session["size"]:
                    raise HTTPException(413, "超过声明的文件大小")
                written += len(chunk)
                await asyncio.to_thread(handle.write, chunk)
        session["received"] += written
        traffic.add(up=written)
        db.upload_save(upload_id, session["folder_id"], session["name"],
                       session["size"], session["received"], int(session["created"]),
                       session.get("group_key"), session.get("part_index"))
    return {"offset": session["received"], "size": session["size"]}


@app.post("/api/uploads/{upload_id}/complete")
async def complete_upload(request: Request, upload_id: str) -> dict[str, Any]:
    guard(request, mutation=True)
    lock = UPLOAD_LOCKS.setdefault(upload_id, asyncio.Lock())
    async with lock:
        session = UPLOAD_SESSIONS.get(upload_id)
        if not session:
            existing = db.job_get(upload_id)
            if existing:
                return {"id": existing["id"], "name": existing["name"], "total": existing["total"]}
            live = backend.jobs.get(upload_id)
            if live is not None:
                return {"id": live.id, "name": live.name, "total": live.total}
            raise HTTPException(404, "上传会话不存在")
        upload = session.get("upload")
        if upload is not None:
            try:
                result = await upload.finish()
            except StreamError as error:
                raise HTTPException(error.code, error.message)
            return {"id": result["id"], "name": result["name"],
                    "total": result["total"], "stream": True}
        actual = session["path"].stat().st_size
        if session["received"] != session["size"] or actual != session["size"]:
            raise HTTPException(409, f"还差 {session['size'] - session['received']} 字节")
        job = UploadJob(id=upload_id, name=session["name"], folder_id=session["folder_id"],
                        total=session["size"], group_key=session.get("group_key"),
                        part_index=session.get("part_index"),
                        part_count=session.get("part_count"))
        db.job_save(job)
        await backend.enqueue_upload(job)
        UPLOAD_SESSIONS.pop(upload_id, None)
        db.upload_delete(upload_id)
    return {"id": job.id, "name": job.name, "total": job.total}


@app.delete("/api/uploads/{upload_id}")
async def abort_upload(request: Request, upload_id: str) -> dict[str, Any]:
    guard(request, mutation=True)
    lock = UPLOAD_LOCKS.setdefault(upload_id, asyncio.Lock())
    async with lock:
        session = UPLOAD_SESSIONS.pop(upload_id, None)
        db.upload_delete(upload_id)
        if session:
            upload = session.get("upload")
            if upload is not None:
                await upload.cancel()
            try:
                if session.get("path") is not None:
                    session["path"].unlink()
            except OSError:
                pass
            if session.get("group_key") is not None and session.get("part_index") is not None:
                db.part_group_set_state(session["group_key"], session["part_index"], "pending")
    return {"ok": True}


# ---- jobs ----------------------------------------------------------------

def _job_dict(job) -> dict[str, Any]:
    return {"id": job.id, "name": job.name, "state": job.state, "sent": job.sent,
            "total": job.total, "message_id": job.message_id, "error": job.error,
            "part_index": getattr(job, "part_index", None),
            "part_count": getattr(job, "part_count", None)}


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str) -> dict[str, Any]:
    guard(request)
    job = backend.jobs.get(job_id)
    if job:
        return _job_dict(job)
    row = db.job_get(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return {"id": row["id"], "name": row["name"], "state": row["state"], "sent": row["sent"],
            "total": row["total"], "message_id": row["message_id"], "error": row["error"],
            "part_index": row.get("part_index"), "part_count": row.get("part_count")}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str) -> dict[str, Any]:
    guard(request, mutation=True)
    row = db.job_get(job_id)
    if not row or row["state"] != "error":
        raise HTTPException(409, "这个任务不能重试")
    path = config.TMP_DIR / job_id
    if not path.exists():
        raise HTTPException(410, "临时文件已删除，请重新上传")
    if not db.job_claim_retry(job_id):
        raise HTTPException(409, "任务状态已变化")
    job = UploadJob(id=job_id, name=row["name"], folder_id=row["folder_id"], total=row["total"],
                    group_key=row.get("group_key"), part_index=row.get("part_index"),
                    part_count=row.get("part_count"))
    db.job_save(job)
    await backend.enqueue_upload(job)
    return {"id": job.id, "state": "queued"}


@app.get("/api/jobs")
async def job_list(request: Request) -> dict[str, Any]:
    guard(request)
    jobs = [_job_dict(j) for j in list(backend.jobs.values())[-50:]]
    known = {job["id"] for job in jobs}
    for row in db.job_list(50):
        if row["id"] not in known:
            jobs.append({"id": row["id"], "name": row["name"], "state": row["state"],
                         "sent": row["sent"], "total": row["total"],
                         "message_id": row["message_id"], "error": row["error"],
                         "part_index": row.get("part_index"),
                         "part_count": row.get("part_count")})
    return {"jobs": jobs}



# ---- dashboard stats -----------------------------------------------------

def _traffic_series(days: int) -> list[dict[str, Any]]:
    rows = {row["day"]: row for row in db.traffic_days(days)}
    today = datetime.date.today()
    out: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        row = rows.get(day)
        out.append({"day": day,
                    "up": int(row["up_bytes"]) if row else 0,
                    "down": int(row["down_bytes"]) if row else 0,
                    "up_files": int(row["up_files"]) if row else 0,
                    "down_files": int(row["down_files"]) if row else 0})
    pending = traffic.snapshot()
    if out:
        out[-1]["up"] += pending["up"]
        out[-1]["down"] += pending["down"]
        out[-1]["up_files"] += pending["up_files"]
        out[-1]["down_files"] += pending["down_files"]
    return out


async def _stats_payload(days: int) -> dict[str, Any]:
    folders = await backend.folders()
    stored = db.storage_rows()
    by_folder: list[dict[str, Any]] = []
    total_bytes = 0
    total_files = 0
    for folder in folders:
        row = stored.get(int(folder["id"]), {})
        size = int(row.get("bytes", 0))
        count = int(row.get("files", 0))
        total_bytes += size
        total_files += count
        by_folder.append({"id": folder["id"], "name": folder["name"],
                          "bytes": size, "files": count})
    by_folder.sort(key=lambda item: item["bytes"], reverse=True)
    totals = db.traffic_totals()
    pending = traffic.snapshot()
    totals["up"] += pending["up"]
    totals["down"] += pending["down"]
    totals["up_files"] += pending["up_files"]
    totals["down_files"] += pending["down_files"]
    disk = shutil.disk_usage(config.DATA_DIR)
    return {
        "storage": {"used_bytes": total_bytes, "files": total_files,
                    "folders": len(folders), "quota_bytes": config.STORAGE_QUOTA_BYTES,
                    "by_folder": by_folder,
                    "updated": max((int(r.get("updated", 0)) for r in stored.values()), default=0)},
        "traffic": {"days": _traffic_series(days), "totals": totals,
                    "today": _traffic_series(1)[-1]},
        "vps_disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "cache": media_cache.stats(),
        "jobs": len(backend.jobs),
        "uploads": len(UPLOAD_SESSIONS),
    }


@app.get("/api/stats")
async def stats(request: Request, days: int = 30) -> dict[str, Any]:
    guard(request)
    return await _stats_payload(max(1, min(int(days), 365)))


@app.post("/api/stats/refresh")
async def stats_refresh(request: Request) -> dict[str, Any]:
    guard(request, mutation=True)
    await _refresh_storage()
    return {"ok": True}


# ---- status --------------------------------------------------------------

@app.get("/api/status")
async def system_status(request: Request) -> dict[str, Any]:
    guard(request)
    disk = shutil.disk_usage(config.DATA_DIR)
    return {
        "telegram": await backend.status(),
        "cache": media_cache.stats(),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "jobs": len(backend.jobs),
        "uploads": len(UPLOAD_SESSIONS),
    }


# ---- media ---------------------------------------------------------------

def content_disposition(name: str, inline: bool = False) -> str:
    from urllib.parse import quote
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name) or "file"
    return f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


def _416(size: int) -> Response:
    return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})


def counting_stream(stream):
    """Wrap a download iterator so every byte we actually send is accounted."""
    async def generator():
        sent = 0
        try:
            async for chunk in stream:
                if not chunk:
                    continue
                sent += len(chunk)
                traffic.add(down=len(chunk))
                yield chunk
        finally:
            if sent:
                traffic.add(down_files=1)
    return generator()


async def media_response(folder_id: int, message_id: int, request: Request,
                         inline: bool) -> Response:
    info = await backend.file_info(folder_id, message_id)
    size = int(info["size"])
    file_id = int(info.get("id", message_id))
    etag = f'"{file_id}-{size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    try:
        rng = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return _416(size)
    if rng and not if_range_matches(request.headers.get("if-range"), etag):
        rng = None
    start, length = rng if rng else (0, size)

    media_type = info["mime"] or "application/octet-stream"
    if media_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(info["name"])
        if guessed:
            media_type = guessed
    kind = info.get("kind", "file")
    if inline and kind == "image" and not media_type.startswith("image/"):
        media_type = "image/jpeg"
    if inline and kind not in SHARE_SAFE_INLINE:
        inline = False
    if inline and media_type not in {
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
        "video/mp4", "video/webm", "audio/mpeg", "audio/ogg", "audio/mp4",
        "application/pdf",
    }:
        # SVG/HTML/XML must never render on our origin.
        inline = False
        media_type = "application/octet-stream"

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(info["name"], inline=inline),
        "Cache-Control": "private, max-age=86400",
        "ETag": etag,
    }
    if rng:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
    headers["Content-Length"] = str(length)
    status = 206 if rng else 200
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media_type)
    if backend.name == "telegram":
        cache_key = f"{folder_id}_{file_id}_{info.get('date', 0)}"
        fetcher = lambda offset, limit: backend.download_iter(folder_id, message_id, offset, limit)
        stream = media_cache.stream(cache_key, size, start, length, fetcher)
        if inline and kind in ("video", "audio") and config.PREBUFFER_MB > 0:
            # Playback reads ahead through ranges; warm a bounded window so the
            # player does not stall waiting on Telegram at every block boundary.
            media_cache.warm(
                cache_key, size, fetcher, start=start,
                max_bytes=config.PREBUFFER_MB * 1024 * 1024,
            )
    else:
        stream = backend.download_iter(folder_id, message_id, start, length)
    return StreamingResponse(counting_stream(stream), status_code=status,
                             headers=headers, media_type=media_type)



@app.get("/api/folders/{folder_id}/files/{message_id}/manifest")
async def file_manifest(request: Request, folder_id: int, message_id: int) -> dict[str, Any]:
    """Part list for browser-side download/merge.

    Offsets and sizes are logical-file coordinates; the client asks the normal
    Range endpoint for each slice and writes them to disk in order.
    """
    guard(request)
    info = await backend.file_info(folder_id, message_id)
    size = int(info["size"])
    group = db.group_by_message(folder_id, message_id)
    out: list[dict[str, Any]] = []
    if group and int(group["parts_count"]) > 1:
        position = 0
        for index, part in enumerate(db.group_parts(folder_id, group["first_message_id"])):
            part_size = int(part["part_size"])
            out.append({"index": index, "message_id": int(part["message_id"]),
                        "offset": position, "size": part_size})
            position += part_size
    else:
        out.append({"index": 0, "message_id": int(info.get("id", message_id)),
                    "offset": 0, "size": size})
    return {"name": info["name"], "size": size, "mime": info.get("mime", ""),
            "kind": info.get("kind", "file"), "parts": out,
            "part_bytes": config.TELEGRAM_PART_BYTES}


@app.get("/api/folders/{folder_id}/files/{message_id}/stream")
async def stream_media(request: Request, folder_id: int, message_id: int) -> Response:
    guard(request)
    return await media_response(folder_id, message_id, request, inline=True)


@app.get("/api/folders/{folder_id}/files/{message_id}/download")
async def download_media(request: Request, folder_id: int, message_id: int) -> Response:
    guard(request)
    return await media_response(folder_id, message_id, request, inline=False)


@app.head("/api/folders/{folder_id}/files/{message_id}/stream")
async def head_stream(request: Request, folder_id: int, message_id: int) -> Response:
    guard(request)
    return await media_response(folder_id, message_id, request, inline=True)


@app.head("/api/folders/{folder_id}/files/{message_id}/download")
async def head_download(request: Request, folder_id: int, message_id: int) -> Response:
    guard(request)
    return await media_response(folder_id, message_id, request, inline=False)


@app.get("/api/folders/{folder_id}/files/{message_id}/thumb")
async def thumbnail(request: Request, folder_id: int, message_id: int) -> Response:
    guard(request)
    payload = await backend.thumbnail(folder_id, message_id)
    if not payload:
        return Response(status_code=404)
    return Response(payload, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


# ---- share links ---------------------------------------------------------

@app.post("/api/share")
async def create_share(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    folder_id = int(payload.get("folder_id") or 0)
    message_id = int(payload.get("message_id") or 0)
    hours = int(payload.get("hours") or 0)
    password = str(payload.get("password") or "")
    if message_id:
        info = await backend.file_info(folder_id, message_id)
        name = info["name"]
    else:
        folders = {f["id"]: f for f in await backend.folders()}
        if folder_id not in folders:
            raise HTTPException(404, "文件夹不存在")
        name = folders[folder_id]["name"]
    token = uuid.uuid4().hex
    expires = int(time.time()) + hours * 3600 if hours > 0 else None
    password_hash = security.hash_password(password) if password else None
    db.create_share(token, folder_id, message_id, expires, password_hash)
    logger.info("share created folder=%s message=%s expiry=%s", folder_id, message_id, expires)
    return {"token": token, "url": f"/s/{token}", "name": name, "expires_at": expires,
            "kind": "file" if message_id else "folder"}


@app.get("/api/shares")
async def list_shares(request: Request) -> dict[str, Any]:
    guard(request)
    return {"shares": db.list_shares()}


@app.delete("/api/share/{token}")
async def delete_share(request: Request, token: str) -> dict[str, Any]:
    guard(request, mutation=True)
    db.delete_share(token)
    return {"ok": True}


def _share_ok(request: Request, share: dict[str, Any]) -> bool:
    if not share.get("password_hash"):
        return True
    cookie = request.cookies.get(f"{SHARE_COOKIE}_{share['token']}")
    return security.verify_share(share["token"], cookie)


def _share_expired(share: dict[str, Any]) -> bool:
    return bool(share["expires_at"]) and share["expires_at"] < time.time()


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _unlock_form(token: str, lang: str) -> str:
    return f"""<!doctype html><html lang="{lang}"><meta charset="utf-8"><title>{i18n.t(lang, "need_password")}</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#171a21;padding:28px;border-radius:14px;width:280px}}
input{{width:100%;padding:10px;margin:12px 0;border-radius:8px;border:1px solid #2c3140;background:#0f1115;color:#fff;box-sizing:border-box}}
button{{width:100%;padding:10px;border:0;border-radius:8px;background:#4f8cff;color:#fff;font-size:15px}}</style>
<form method="post" action="/s/{token}/unlock?lang={lang}"><h3>{i18n.t(lang, "password_protected")}</h3>
<input type="password" name="password" placeholder="{i18n.t(lang, "access_password")}" autofocus><button>{i18n.t(lang, "unlock")}</button></form>"""


def _size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    return f"{size / 1024:.1f} KB"


def _file_page(token: str, info: dict[str, Any], lang: str, base: str | None = None) -> str:
    name = _esc(info["name"])
    raw = base or f"/s/{token}/raw"
    preview = ""
    if info["kind"] == "image":
        preview = f'<img src="{raw}?inline=1" style="max-width:100%;border-radius:12px;margin-bottom:18px">'
    elif info["kind"] in ("video", "audio"):
        tag = "video" if info["kind"] == "video" else "audio"
        preview = (f'<{tag} controls src="{raw}?inline=1" '
                   f'style="max-width:100%;width:640px;border-radius:12px;margin-bottom:18px"></{tag}>')
    return f"""<!doctype html><html lang="{lang}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{background:#171a21;padding:28px;border-radius:16px;max-width:720px;width:90%;text-align:center}}
a.btn{{display:inline-block;margin-top:14px;padding:10px 18px;border-radius:9px;background:#4f8cff;color:#fff;text-decoration:none}}
.muted{{color:#8b93a7;font-size:13px}}</style>
<div class="card">{preview}<h3>{name}</h3><div class="muted">{_size_text(info["size"])}</div>
<a class="btn" href="{raw}">{i18n.t(lang, "download")}</a></div>"""


def _folder_page(token: str, items: list[dict[str, Any]], lang: str) -> str:
    rows = "".join(
        f'<li><a href="/s/{token}/{item["id"]}?lang={lang}">{_esc(item["name"])}</a>'
        f'<span class="muted"> {_size_text(item["size"])}</span></li>'
        for item in items
    )
    return f"""<!doctype html><html lang="{lang}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{i18n.t(lang, "shared_folder")}</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e6e6;margin:0;padding:32px}}
.card{{background:#171a21;padding:24px;border-radius:16px;max-width:760px;margin:0 auto}}
li{{margin:10px 0}} a{{color:#4f8cff;text-decoration:none}} .muted{{color:#8b93a7;font-size:13px}}</style>
<div class="card"><h3>{i18n.t(lang, "shared_folder")}</h3><ul>{rows or f'<li class="muted">{i18n.t(lang, "empty_folder")}</li>'}</ul></div>"""


def _share_error(lang: str, key: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="{lang}"><meta charset="utf-8">'
        f'<body style="font-family:system-ui;background:#0f1115;color:#e6e6e6">'
        f'<h1>{i18n.t(lang, key)}</h1></body>', status_code=status)


@app.get("/s/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str) -> HTMLResponse:
    lang = i18n.detect(request)
    share = db.get_share(token)
    if not share:
        return _share_error(lang, "link_missing", 404)
    if _share_expired(share):
        return _share_error(lang, "link_expired", 410)
    if not _share_ok(request, share):
        return HTMLResponse(_unlock_form(token, lang))
    try:
        if share["message_id"]:
            info = await backend.file_info(share["folder_id"], share["message_id"])
            return HTMLResponse(_file_page(token, info, lang))
        files = await backend.files(share["folder_id"], offset=0, limit=200)
        return HTMLResponse(_folder_page(token, files["items"], lang))
    except BackendError:
        return _share_error(lang, "file_deleted", 404)


@app.post("/s/{token}/unlock")
async def share_unlock(request: Request, token: str) -> Response:
    share = db.get_share(token)
    if not share:
        raise HTTPException(404, "链接不存在")
    if _share_expired(share):
        raise HTTPException(410, "链接已过期")
    ip = security.client_ip(request)
    limit_key = f"share:{token}:{ip}"
    if not security.share_limiter.allow(limit_key, limit=8, window=600):
        raise HTTPException(429, "尝试次数太多，稍后再试")
    form = await request.form()
    password = str(form.get("password") or "")
    if not share.get("password_hash") or not security._check_hash(password, share["password_hash"]):
        security.share_limiter.record(limit_key)
        return RedirectResponse(f"/s/{token}", status_code=303)
    lang = i18n.detect(request)
    response = RedirectResponse(f"/s/{token}?lang={lang}", status_code=303)
    response.set_cookie("hd_lang", lang, max_age=86400 * 365, httponly=False,
                        samesite="lax", path="/")
    response.set_cookie(
        f"{SHARE_COOKIE}_{token}", security.issue_share(token),
        max_age=3600, httponly=True, samesite="lax",
        secure=security.is_https(request), path=f"/s/{token}",
    )
    return response


@app.get("/s/{token}/raw")
async def share_raw(request: Request, token: str, inline: int = 0) -> Response:
    share = db.get_share(token)
    if not share:
        raise HTTPException(404, "链接不存在")
    if _share_expired(share):
        raise HTTPException(410, "链接已过期")
    if not _share_ok(request, share):
        raise HTTPException(403, "需要密码")
    if not share["message_id"]:
        raise HTTPException(404, "文件夹分享没有直链")
    db.bump_share_downloads(token)
    return await media_response(share["folder_id"], share["message_id"], request,
                                inline=bool(inline))


@app.get("/s/{token}/{message_id}", response_class=HTMLResponse)
async def share_item_page(request: Request, token: str, message_id: int) -> HTMLResponse:
    lang = i18n.detect(request)
    share = db.get_share(token)
    if not share or share["message_id"]:
        raise HTTPException(404, "链接不存在")
    if _share_expired(share):
        return _share_error(lang, "link_expired", 410)
    if not _share_ok(request, share):
        return HTMLResponse(_unlock_form(token, lang))
    info = await backend.file_info(share["folder_id"], message_id)
    return HTMLResponse(_file_page(token, info, lang, base=f"/s/{token}/{message_id}/raw"))


@app.get("/s/{token}/{message_id}/raw")
async def share_item_raw(request: Request, token: str, message_id: int,
                         inline: int = 0) -> Response:
    share = db.get_share(token)
    if not share or share["message_id"]:
        raise HTTPException(404, "链接不存在")
    if _share_expired(share):
        raise HTTPException(410, "链接已过期")
    if not _share_ok(request, share):
        raise HTTPException(403, "需要密码")
    return await media_response(share["folder_id"], message_id, request, inline=bool(inline))


# ---- runtime settings ----------------------------------------------------

@app.get("/api/settings")
async def get_settings(request: Request) -> JSONResponse:
    guard(request)
    status = await backend.status()
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = 0
    payload = {
        "version": APP_VERSION,
        "site_name": config.SITE_NAME,
        "demo": config.DEMO_MODE,
        "items": settings.describe(),
        "system": {
            "data_dir": str(config.DATA_DIR),
            "disk_total": disk_total,
            "disk_free": disk_free,
            "python": platform.python_version(),
            "uptime": max(0, int(time.time() - STARTED_AT)),
            "telegram": status,
            "cache": media_cache.stats(),
        },
    }
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/settings")
async def update_settings(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        raise HTTPException(400, "没有要保存的设置")
    errors = settings.save(values)
    if errors:
        raise HTTPException(400, "；".join(errors))
    logger.info("settings updated: %s", ", ".join(sorted(values)))
    return {"ok": True, "items": settings.describe()}


@app.post("/api/settings/password")
async def update_password(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    guard(request, mutation=True)
    error = settings.set_admin_password(
        str(payload.get("current") or ""), str(payload.get("new") or "")
    )
    if error:
        raise HTTPException(400, error)
    logger.info("admin password rotated")
    return {"ok": True}


# ---- webdav settings -----------------------------------------------------

@app.get("/api/settings/webdav")
async def webdav_settings(request: Request) -> dict[str, Any]:
    guard(request)
    user, password = webdav.credentials()
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    response = JSONResponse({"user": user, "password": password, "path": f"{prefix}/dav/"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/settings/webdav/rotate")
async def webdav_rotate(request: Request) -> dict[str, Any]:
    guard(request, mutation=True)
    password = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    db.kv_set("webdav_pass", password, secret=True)
    user = db.kv_get("webdav_user") or config.WEBDAV_USER
    return {"user": user, "password": password}
