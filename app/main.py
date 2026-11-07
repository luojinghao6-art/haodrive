"""FastAPI application for seagulldrive (VPS Telegram cloud drive)."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

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

from . import config, db, security, webdav
from .demo_backend import DemoBackend
from .http_range import RangeNotSatisfiable, if_range_matches, parse_range
from .media_cache import MediaCache
from .tg_backend import BackendError, TelegramBackend, UploadJob, friendly_error

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("seagulldrive")

app = FastAPI(title="seagulldrive", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(webdav.router, prefix="/dav")

backend = DemoBackend() if config.DEMO_MODE else TelegramBackend()
media_cache = MediaCache(config.CACHE_DIR / "media", int(config.MEDIA_CACHE_GB * 1024**3))
SHARE_COOKIE = "sd_share"
UPLOAD_SESSIONS: dict[str, dict[str, Any]] = {}
UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}
MAX_UPLOAD_SESSIONS = 32
SHARE_SAFE_INLINE = {"image", "video", "audio", "pdf"}


# ---- lifecycle -----------------------------------------------------------

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
        UPLOAD_SESSIONS[upload_id] = {
            "folder_id": row["folder_id"], "name": row["name"], "size": row["size"],
            "path": path, "received": received, "created": row["created"],
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
                            total=row["total"] or path.stat().st_size)
            await backend.enqueue_upload(job)
            logger.info("requeued job %s (%s)", job.id, job.name)
        else:
            job = UploadJob(id=row["id"], name=row["name"], folder_id=row["folder_id"],
                            total=row["total"], state="error",
                            error="服务重启，临时文件已清理，请重新上传")
            backend.jobs.setdefault(job.id, job)
            db.job_save(job)


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    _restore_uploads()
    db.job_prune()
    await backend.start()
    await _restore_jobs()
    logger.info("seagulldrive started (demo=%s, cache=%s)", config.DEMO_MODE, media_cache.stats())


@app.on_event("shutdown")
async def _shutdown() -> None:
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
    info: dict[str, Any] = {"authenticated": authed, "demo": config.DEMO_MODE}
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
                    await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await upload_file.close()
        job = UploadJob(id=job_id, name=safe_name, folder_id=folder_id, total=total)
        db.job_save(job)
        await backend.enqueue_upload(job)
        jobs.append({"id": job_id, "name": safe_name, "total": total})
    return {"jobs": jobs}


# ---- resumable (chunked) upload -----------------------------------------

def _purge_uploads() -> None:
    cutoff = time.time() - 86400
    for uid, session in list(UPLOAD_SESSIONS.items()):
        if session["created"] < cutoff:
            try:
                session["path"].unlink()
            except OSError:
                pass
            db.upload_delete(uid)
            UPLOAD_SESSIONS.pop(uid, None)
            UPLOAD_LOCKS.pop(uid, None)


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
        db.upload_save(upload_id, session["folder_id"], session["name"],
                       session["size"], session["received"], int(session["created"]))
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
            raise HTTPException(404, "上传会话不存在")
        actual = session["path"].stat().st_size
        if session["received"] != session["size"] or actual != session["size"]:
            raise HTTPException(409, f"还差 {session['size'] - session['received']} 字节")
        job = UploadJob(id=upload_id, name=session["name"], folder_id=session["folder_id"],
                        total=session["size"])
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
            try:
                session["path"].unlink()
            except OSError:
                pass
    return {"ok": True}


# ---- jobs ----------------------------------------------------------------

def _job_dict(job) -> dict[str, Any]:
    return {"id": job.id, "name": job.name, "state": job.state, "sent": job.sent,
            "total": job.total, "message_id": job.message_id, "error": job.error}


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
            "total": row["total"], "message_id": row["message_id"], "error": row["error"]}


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
    job = UploadJob(id=job_id, name=row["name"], folder_id=row["folder_id"], total=row["total"])
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
                         "message_id": row["message_id"], "error": row["error"]})
    return {"jobs": jobs}


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


async def media_response(folder_id: int, message_id: int, request: Request,
                         inline: bool) -> Response:
    info = await backend.file_info(folder_id, message_id)
    size = int(info["size"])
    etag = f'"{message_id}-{size}"'
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
        stream = media_cache.stream(
            f"{folder_id}_{message_id}_{info.get('date', 0)}", size, start, length,
            lambda offset, limit: backend.download_iter(folder_id, message_id, offset, limit),
        )
    else:
        stream = backend.download_iter(folder_id, message_id, start, length)
    return StreamingResponse(stream, status_code=status, headers=headers, media_type=media_type)


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


def _unlock_form(token: str) -> str:
    return f"""<!doctype html><meta charset="utf-8"><title>需要密码</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#171a21;padding:28px;border-radius:14px;width:280px}}
input{{width:100%;padding:10px;margin:12px 0;border-radius:8px;border:1px solid #2c3140;background:#0f1115;color:#fff;box-sizing:border-box}}
button{{width:100%;padding:10px;border:0;border-radius:8px;background:#4f8cff;color:#fff;font-size:15px}}</style>
<form method="post" action="/s/{token}/unlock"><h3>此链接受密码保护</h3>
<input type="password" name="password" placeholder="访问密码" autofocus><button>解锁</button></form>"""


def _size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    return f"{size / 1024:.1f} KB"


def _file_page(token: str, info: dict[str, Any], base: str | None = None) -> str:
    name = _esc(info["name"])
    raw = base or f"/s/{token}/raw"
    preview = ""
    if info["kind"] == "image":
        preview = f'<img src="{raw}?inline=1" style="max-width:100%;border-radius:12px;margin-bottom:18px">'
    elif info["kind"] in ("video", "audio"):
        tag = "video" if info["kind"] == "video" else "audio"
        preview = (f'<{tag} controls src="{raw}?inline=1" '
                   f'style="max-width:100%;width:640px;border-radius:12px;margin-bottom:18px"></{tag}>')
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{background:#171a21;padding:28px;border-radius:16px;max-width:720px;width:90%;text-align:center}}
a.btn{{display:inline-block;margin-top:14px;padding:10px 18px;border-radius:9px;background:#4f8cff;color:#fff;text-decoration:none}}
.muted{{color:#8b93a7;font-size:13px}}</style>
<div class="card">{preview}<h3>{name}</h3><div class="muted">{_size_text(info["size"])}</div>
<a class="btn" href="{raw}">下载</a></div>"""


def _folder_page(token: str, items: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<li><a href="/s/{token}/{item["id"]}">{_esc(item["name"])}</a>'
        f'<span class="muted"> {_size_text(item["size"])}</span></li>'
        for item in items
    )
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>共享文件夹</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e6e6e6;margin:0;padding:32px}}
.card{{background:#171a21;padding:24px;border-radius:16px;max-width:760px;margin:0 auto}}
li{{margin:10px 0}} a{{color:#4f8cff;text-decoration:none}} .muted{{color:#8b93a7;font-size:13px}}</style>
<div class="card"><h3>共享文件夹</h3><ul>{rows or '<li class="muted">空文件夹</li>'}</ul></div>"""


@app.get("/s/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str) -> HTMLResponse:
    share = db.get_share(token)
    if not share:
        return HTMLResponse("<h1>链接不存在</h1>", status_code=404)
    if _share_expired(share):
        return HTMLResponse("<h1>链接已过期</h1>", status_code=410)
    if not _share_ok(request, share):
        return HTMLResponse(_unlock_form(token))
    try:
        if share["message_id"]:
            info = await backend.file_info(share["folder_id"], share["message_id"])
            return HTMLResponse(_file_page(token, info))
        files = await backend.files(share["folder_id"], offset=0, limit=200)
        return HTMLResponse(_folder_page(token, files["items"]))
    except BackendError:
        return HTMLResponse("<h1>文件已删除</h1>", status_code=404)


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
    response = RedirectResponse(f"/s/{token}", status_code=303)
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
    share = db.get_share(token)
    if not share or share["message_id"]:
        raise HTTPException(404, "链接不存在")
    if _share_expired(share):
        return HTMLResponse("<h1>链接已过期</h1>", status_code=410)
    if not _share_ok(request, share):
        return HTMLResponse(_unlock_form(token))
    info = await backend.file_info(share["folder_id"], message_id)
    return HTMLResponse(_file_page(token, info, base=f"/s/{token}/{message_id}/raw"))


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
