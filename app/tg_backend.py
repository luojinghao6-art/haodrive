"""Telegram backend: a user account, broadcast channels as folders, messages as files."""
from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession
from telethon.tl.functions.channels import CreateChannelRequest, GetFullChannelRequest
from telethon.tl.types import (
    Channel,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

from . import config, db

logger = logging.getLogger("seagulldrive.tg")

FOLDER_MARK = "[TD]"
FOLDER_ABOUT = "Telegram Drive Storage Folder\n[telegram-drive-folder]"
STREAM_CHUNK = config.STREAM_CHUNK
THUMB_TTL = 7 * 86400


class BackendError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def friendly_error(error: Exception) -> str:
    text = str(error)
    mapping = [
        ("FLOOD_WAIT", "Telegram 限流了，等一会儿再试（FLOOD_WAIT）"),
        ("API_ID_INVALID", "api_id / api_hash 无效"),
        ("API_ID_PUBLISHED_FLOOD", "这个 api_id 是公开过的，去 my.telegram.org 新建一个"),
        ("PHONE_NUMBER_INVALID", "手机号格式不对，要带国际区号"),
        ("PHONE_NUMBER_BANNED", "手机号被 Telegram 封禁"),
        ("PHONE_CODE_INVALID", "验证码错误"),
        ("PHONE_CODE_EXPIRED", "验证码过期了，重新获取"),
        ("PASSWORD_HASH_INVALID", "两步验证密码错误"),
        ("SESSION_PASSWORD_NEEDED", "需要两步验证密码"),
        ("CHANNEL_PRIVATE", "频道不可访问（可能被踢了或者已删除）"),
        ("CHANNEL_INVALID", "频道不存在或没有权限"),
        ("CHAT_WRITE_FORBIDDEN", "没有发消息权限，你是不是频道管理员？"),
        ("CHAT_ADMIN_REQUIRED", "需要频道管理员权限"),
        ("FILE_TOO_LARGE", "文件超过 Telegram 上限（2GB）"),
        ("USER_NOT_PARTICIPANT", "账号不在这个频道里"),
        ("AUTH_KEY_UNREGISTERED", "会话已失效，请重新登录"),
        ("AUTH_KEY_DUPLICATED", "会话在别处被使用了，需要重新登录"),
    ]
    for needle, message in mapping:
        if needle in text.upper():
            return message
    if isinstance(error, errors.FloodWaitError):
        return f"Telegram 限流，等待 {error.seconds} 秒"
    return text


class _SliceReader:
    """Bounded sequential reader over an open file, for uploading one part."""

    def __init__(self, handle, length: int, name: str) -> None:
        self._handle = handle
        self._remaining = length
        self.name = name

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        data = self._handle.read(size)
        self._remaining -= len(data)
        return data

    def seekable(self) -> bool:
        return False


def plan_parts(parts: list[dict[str, Any]], offset: int, length: int) -> list[tuple[int, int, int]]:
    """Map a byte range of the merged file onto (message_id, part_offset, take)."""
    plan: list[tuple[int, int, int]] = []
    position = 0
    end = offset + length
    for part in parts:
        size = int(part.get("size", part.get("part_size", 0)))
        start = position
        stop = position + size
        position = stop
        if stop <= offset or start >= end:
            continue
        local = max(offset, start) - start
        take = min(end, stop) - max(offset, start)
        if take > 0:
            plan.append((int(part.get("id", part.get("message_id"))), local, take))
    return plan


@dataclass
class UploadJob:
    id: str
    name: str
    folder_id: int
    state: str = "queued"  # queued|running|done|error
    sent: int = 0
    total: int = 0
    message_id: int | None = None
    error: str | None = None


def _kind_from_mime(mime: str) -> str:
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


def _kind(message: types.Message) -> str:
    if message.photo:
        return "image"
    doc = message.document
    mime = (message.file.mime_type or "").lower() if message.file else ""
    if doc:
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return "video"
            if isinstance(attr, DocumentAttributeAudio):
                return "audio"
    return _kind_from_mime(mime)


def _file_name(message: types.Message) -> str:
    try:
        if message.file and message.file.name:
            return message.file.name
    except Exception:
        pass
    ext = os.path.splitext(message.file.ext or "")[1] if message.file else ""
    return f"file_{message.id}{ext}"


def _proxy_arg():
    """Telethon wants a mapping; accept socks5://user:pass@host:port in env."""
    if not config.PROXY:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(config.PROXY)
    if not parsed.scheme or not parsed.hostname:
        return config.PROXY
    return {
        "proxy_type": parsed.scheme,
        "addr": parsed.hostname,
        "port": parsed.port or 1080,
        "username": parsed.username,
        "password": parsed.password,
        "rdns": True,
    }


def forwarded_id(result: Any) -> int | None:
    """Telethon returns a Message for one id and a list for several."""
    if result is None:
        return None
    if isinstance(result, list):
        return result[0].id if result else None
    return getattr(result, "id", None)


def describe(message: types.Message) -> dict[str, Any]:
    f = message.file
    name = _file_name(message)
    mime = (f.mime_type if f else None) or ""
    if mime in ("", "application/octet-stream"):
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            mime = guessed
    kind = _kind(message)
    if kind == "file":
        kind = _kind_from_mime(mime)
    return {
        "id": message.id,
        "name": name,
        "size": int(f.size or 0) if f else 0,
        "mime": mime,
        "date": int(message.date.timestamp()) if message.date else 0,
        "kind": kind,
        "has_thumb": bool(message.photo or (message.document and message.document.thumbs)),
        "duration": int(getattr(f, "duration", 0) or 0) if f else 0,
        "width": int(getattr(f, "width", 0) or 0) if f else 0,
        "height": int(getattr(f, "height", 0) or 0) if f else 0,
        "caption": (message.message or "")[:200],
    }


class TelegramBackend:
    name = "telegram"

    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self._client_lock = asyncio.Lock()
        self._entities: dict[int, Any] = {}
        self._msg_cache: dict[tuple[int, int], tuple[float, Any]] = {}
        self._phone: str | None = None
        self._code_hash: str | None = None
        self.state = "unconfigured"  # unconfigured|awaiting_code|awaiting_password|ready|error
        self.last_error: str | None = None
        self._upload_queue: asyncio.Queue[UploadJob] | None = None
        self.jobs: dict[str, UploadJob] = {}
        self._worker: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        api_id = db.kv_get("tg_api_id")
        session = db.kv_get("tg_session", secret=True)
        if not api_id or not session:
            self.state = "unconfigured"
            return
        try:
            await self._connect(int(api_id), session)
            self.state = "ready"
        except Exception as error:
            text = str(error).upper()
            if any(marker in text for marker in
                   ("AUTH_KEY_UNREGISTERED", "AUTH_KEY_INVALID", "SESSION_REVOKED",
                    "USER_DEACTIVATED", "SESSION_EXPIRED")):
                db.kv_set("tg_session", None, secret=True)
                self.state = "unconfigured"
            else:
                # Network/proxy trouble at boot must not log the user out.
                self.state = "error"
            self.last_error = friendly_error(error)

    async def _connect(self, api_id: int, session: str) -> None:
        async with self._client_lock:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            client = TelegramClient(
                StringSession(session),
                api_id,
                db.kv_get("tg_api_hash", secret=True) or "",
                proxy=_proxy_arg(),
                connection_retries=5,
                auto_reconnect=True,
                app_version="seagulldrive 1.0",
                device_model="seagulldrive",
                system_version="Linux",
            )
            await client.connect()
            if not await client.is_user_authorized():
                raise BackendError("会话未授权")
            self.client = client
            self._entities.clear()

    def _client(self) -> TelegramClient:
        if self.client is None:
            raise BackendError("Telegram 还没登录", 401)
        return self.client

    async def status(self) -> dict[str, Any]:
        me = None
        if self.client is not None:
            try:
                user = await self.client.get_me()
                if user:
                    me = {
                        "id": user.id,
                        "name": " ".join(filter(None, [user.first_name, user.last_name])),
                        "username": user.username,
                    }
            except Exception:
                pass
        return {
            "state": self.state,
            "user": me,
            "error": self.last_error,
            "has_credentials": bool(db.kv_get("tg_api_id")),
            "proxy": bool(config.PROXY),
        }

    # ---- login -----------------------------------------------------------

    def _new_client(self, api_id: int, api_hash: str, session: StringSession) -> TelegramClient:
        return TelegramClient(
            session,
            api_id,
            api_hash,
            proxy=_proxy_arg(),
            device_model="seagulldrive",
            system_version="Linux",
            app_version="seagulldrive 1.0",
        )

    async def begin_login(self, api_id: int, api_hash: str, phone: str) -> dict[str, Any]:
        if not api_id or not api_hash or not phone:
            raise BackendError("api_id / api_hash / 手机号都要填")
        client = self._new_client(api_id, api_hash, StringSession())
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except Exception as error:
            await client.disconnect()
            raise BackendError(friendly_error(error))
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = client
        self._phone = phone
        self._code_hash = sent.phone_code_hash
        db.kv_set("tg_api_id", str(api_id))
        db.kv_set("tg_api_hash", api_hash, secret=True)
        self.state = "awaiting_code"
        self.last_error = None
        return {"state": self.state, "code_type": type(sent.type).__name__}

    async def submit_code(self, code: str) -> dict[str, Any]:
        client = self._client()
        if not self._phone or not self._code_hash:
            raise BackendError("先获取验证码", 409)
        try:
            await client.sign_in(self._phone, code, phone_code_hash=self._code_hash)
        except errors.SessionPasswordNeededError:
            self.state = "awaiting_password"
            return {"state": self.state}
        except Exception as error:
            raise BackendError(friendly_error(error))
        await self._finish_login()
        return {"state": self.state}

    async def submit_password(self, password: str) -> dict[str, Any]:
        client = self._client()
        try:
            await client.sign_in(password=password)
        except Exception as error:
            raise BackendError(friendly_error(error))
        await self._finish_login()
        return {"state": self.state}

    async def _finish_login(self) -> None:
        client = self._client()
        session = client.session.save()
        db.kv_set("tg_session", session, secret=True)
        self.state = "ready"
        self.last_error = None
        self._entities.clear()

    async def logout(self) -> None:
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        db.kv_set("tg_session", None, secret=True)
        self.state = "unconfigured"
        self._entities.clear()

    # ---- entities and folders -------------------------------------------

    async def _entity(self, folder_id: int) -> Any:
        if folder_id in self._entities:
            return self._entities[folder_id]
        client = self._client()
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if isinstance(ent, Channel):
                self._entities[ent.id] = ent
        if folder_id in self._entities:
            return self._entities[folder_id]
        raise BackendError("频道不存在或已退出", 404)

    def _is_folder(self, channel: Channel) -> bool:
        title = (channel.title or "").lower()
        return FOLDER_MARK.lower() in title

    async def folders(self) -> list[dict[str, Any]]:
        client = self._client()
        known = {row["channel_id"]: row for row in db.folder_rows()}
        found: dict[int, dict[str, Any]] = {}
        off_marker: list[Channel] = []
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if not isinstance(ent, Channel):
                continue
            self._entities[ent.id] = ent
            if self._is_folder(ent):
                cleaned = ent.title.replace(FOLDER_MARK, "").replace("[td]", "").strip()
                found[ent.id] = self._folder_dict(ent, cleaned)
            elif ent.id in known:
                # Already recognised earlier: never re-probe, never prune.
                found[ent.id] = self._folder_dict(ent, known[ent.id]["name"])
            elif getattr(ent, "creator", False) or getattr(ent, "admin_rights", None):
                off_marker.append(ent)
        # Unknown channels owned by this account may carry the legacy About marker.
        for ent in off_marker[:30]:
            try:
                full = await client(GetFullChannelRequest(channel=ent))
                about = getattr(full.full_chat, "about", "") or ""
                if "[telegram-drive-folder]" in about:
                    found[ent.id] = self._folder_dict(ent, ent.title)
            except Exception:
                continue
        for channel_id, info in found.items():
            db.upsert_folder(channel_id, info["name"], info["username"], info["is_public"])
        order = {row["channel_id"]: row["display_order"] for row in db.folder_rows()}
        result = sorted(found.values(), key=lambda f: (order.get(f["id"], 10**9), f["name"].lower()))
        return result

    def _folder_dict(self, channel: Channel, name: str) -> dict[str, Any]:
        return {
            "id": channel.id,
            "name": name or (channel.title or "未命名"),
            "username": channel.username,
            "is_public": bool(channel.username),
        }

    async def create_folder(self, name: str) -> dict[str, Any]:
        client = self._client()
        name = name.strip()
        if not name:
            raise BackendError("文件夹名字不能为空")
        title = f"{name} {FOLDER_MARK}"
        try:
            result = await client(CreateChannelRequest(
                title=title, about=FOLDER_ABOUT, broadcast=True, megagroup=False
            ))
        except Exception as error:
            raise BackendError(friendly_error(error))
        channel = result.chats[0]
        self._entities[channel.id] = channel
        db.upsert_folder(channel.id, name, channel.username, bool(channel.username))
        return self._folder_dict(channel, name)

    async def delete_folder(self, folder_id: int) -> None:
        client = self._client()
        entity = await self._entity(folder_id)
        try:
            await client(functions.channels.DeleteChannelRequest(channel=entity))
        except Exception as error:
            raise BackendError(friendly_error(error))
        db.remove_folder(folder_id)
        self._entities.pop(folder_id, None)

    # ---- files -----------------------------------------------------------

    async def files(self, folder_id: int, offset: int = 0, limit: int = 60,
                    search: str | None = None) -> dict[str, Any]:
        client = self._client()
        entity = await self._entity(folder_id)
        limit = max(1, min(limit, 200))
        items: list[dict[str, Any]] = []
        iterated = 0
        last_id = 0
        kwargs: dict[str, Any] = {"limit": limit}
        if search:
            kwargs["search"] = search
        if offset:
            kwargs["offset_id"] = offset
        try:
            async for message in client.iter_messages(entity, **kwargs):
                iterated += 1
                last_id = message.id
                if not message.media:
                    continue
                info = describe(message)
                if info["size"] <= 0 and info["kind"] not in ("image", "video"):
                    continue
                items.append(info)
        except Exception as error:
            raise BackendError(friendly_error(error))
        # Continue paging while Telegram returned a full page, even if that page
        # held fewer media messages than the limit.
        # Local display-name overlay: Telegram cannot rename, so the web app
        # keeps its own name for each message.
        groups, hidden = db.groups_for_folder(folder_id)
        if groups:
            items = [item for item in items if item["id"] not in hidden]
            for item in items:
                group = groups.get(item["id"])
                if not group:
                    continue
                item.update({
                    "size": group["size"],
                    "name": group["name"],
                    "mime": group["mime"] or item.get("mime", ""),
                    "kind": group["kind"] or item.get("kind", "file"),
                    "parts": group["parts_count"],
                })
        names = db.display_names(folder_id, [item["id"] for item in items])
        seen = set()
        for item in items:
            seen.add(item["id"])
            if item["id"] in names:
                item["name"] = names[item["id"]]
        if search:
            candidates = db.search_display_names(folder_id, search)
            for first_id in db.search_groups(folder_id, search):
                if first_id not in candidates:
                    candidates.append(first_id)
            for message_id in candidates:
                if message_id in seen:
                    continue
                try:
                    message = await self._message(folder_id, message_id)
                except BackendError:
                    continue
                info = describe(message)
                group = groups.get(message_id)
                if group:
                    info.update({
                        "size": group["size"],
                        "name": group["name"],
                        "mime": group["mime"] or info.get("mime", ""),
                        "kind": group["kind"] or info.get("kind", "file"),
                        "parts": group["parts_count"],
                    })
                override = db.display_names(folder_id, [message_id]).get(message_id)
                if override:
                    info["name"] = override
                items.append(info)
                seen.add(message_id)
        return {"items": items, "next_offset": last_id if iterated >= limit else 0}

    async def _message(self, folder_id: int, message_id: int) -> types.Message:
        key = (folder_id, message_id)
        cached = self._msg_cache.get(key)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        client = self._client()
        entity = await self._entity(folder_id)
        message = await client.get_messages(entity, ids=message_id)
        if not message or not message.media:
            raise BackendError("文件不存在", 404)
        if len(self._msg_cache) > 512:
            self._msg_cache.clear()
        self._msg_cache[key] = (time.time(), message)
        return message

    async def file_info(self, folder_id: int, message_id: int) -> dict[str, Any]:
        group = db.group_by_message(folder_id, message_id)
        target = group["first_message_id"] if group else message_id
        info = describe(await self._message(folder_id, target))
        if group:
            info.update({
                "size": group["size"],
                "name": group["name"],
                "mime": group["mime"] or info.get("mime", ""),
                "kind": group["kind"] or info.get("kind", "file"),
                "parts": group["parts_count"],
            })
        override = db.display_names(folder_id, [target]).get(target)
        if override:
            info["name"] = override
        return info

    async def download_iter(self, folder_id: int, message_id: int, offset: int = 0,
                            length: int | None = None) -> AsyncIterator[bytes]:
        group = db.group_by_message(folder_id, message_id)
        if group:
            parts = db.group_parts(folder_id, group["first_message_id"])
            if length is None:
                length = max(0, group["size"] - offset)
            for part_id, part_offset, take in plan_parts(parts, offset, length):
                async for chunk in self._download_message(folder_id, part_id, part_offset, take):
                    yield chunk
            return
        async for chunk in self._download_message(folder_id, message_id, offset, length):
            yield chunk

    async def _download_message(self, folder_id: int, message_id: int, offset: int = 0,
                                length: int | None = None) -> AsyncIterator[bytes]:
        client = self._client()
        message = await self._message(folder_id, message_id)
        size = int(message.file.size or 0) if message.file else 0
        if length is None:
            length = max(0, size - offset)
        # Telegram wants 4096-aligned offsets; skip the unaligned head ourselves.
        aligned = offset - (offset % 4096)
        skip = offset - aligned
        remaining = length + skip
        try:
            async for chunk in client.iter_download(
                message, offset=aligned, limit=remaining,
                request_size=STREAM_CHUNK, chunk_size=STREAM_CHUNK,
            ):
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                if not chunk:
                    continue
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                remaining -= len(chunk)
                yield chunk
                if remaining <= 0:
                    break
        except Exception as error:
            raise BackendError(friendly_error(error))

    async def thumbnail(self, folder_id: int, message_id: int) -> bytes | None:
        group = db.group_by_message(folder_id, message_id)
        target = group["first_message_id"] if group else message_id
        cache = config.CACHE_DIR / f"{folder_id}_{target}.thumb"
        if cache.exists() and time.time() - cache.stat().st_mtime < THUMB_TTL:
            return cache.read_bytes()
        message = await self._message(folder_id, target)
        payload = b""
        for thumb in (-1, 0):
            buffer = io.BytesIO()
            try:
                await self._client().download_media(message, file=buffer, thumb=thumb)
            except Exception:
                continue
            payload = buffer.getvalue()
            if payload:
                break
        if not payload:
            return None
        cache.write_bytes(payload)
        return payload

    async def delete_file(self, folder_id: int, message_id: int) -> None:
        client = self._client()
        entity = await self._entity(folder_id)
        group = db.group_by_message(folder_id, message_id)
        if group:
            ids = [p["message_id"] for p in db.group_parts(folder_id, group["first_message_id"])]
        else:
            ids = [message_id]
        try:
            await client.delete_messages(entity, ids)
        except Exception as error:
            raise BackendError(friendly_error(error))
        for part_id in ids:
            self._msg_cache.pop((folder_id, part_id), None)
            db.clear_display_name(folder_id, part_id)
        if group:
            db.group_delete(folder_id, group["first_message_id"])

    async def move_file(self, src_folder: int, message_id: int, dst_folder: int) -> int | None:
        client = self._client()
        src = await self._entity(src_folder)
        dst = await self._entity(dst_folder)
        group = db.group_by_message(src_folder, message_id)
        try:
            if group:
                parts = db.group_parts(src_folder, group["first_message_id"])
                ids = [p["message_id"] for p in parts]
                result = await client.forward_messages(dst, ids, src)
                messages = result if isinstance(result, list) else [result]
                new_ids = [m.id for m in messages if m is not None]
                if len(new_ids) != len(ids):
                    raise BackendError("转发失败，源文件已保留")
                await client.delete_messages(src, ids)
                db.group_save(
                    dst_folder, new_ids[0], group["name"], group["size"],
                    group["mime"], group["kind"],
                    [{"id": new_id, "size": part["part_size"]}
                     for new_id, part in zip(new_ids, parts)],
                )
                db.group_delete(src_folder, group["first_message_id"])
                db.move_display_name(src_folder, group["first_message_id"], dst_folder, new_ids[0])
                for part_id in ids:
                    self._msg_cache.pop((src_folder, part_id), None)
                return new_ids[0]
            forwarded = await client.forward_messages(dst, message_id, src)
            new_id = forwarded_id(forwarded)
            if not new_id:
                raise BackendError("转发失败，源文件已保留")
            await client.delete_messages(src, [message_id])
            if new_id:
                db.move_display_name(src_folder, message_id, dst_folder, new_id)
            self._msg_cache.pop((src_folder, message_id), None)
            return new_id
        except Exception as error:
            raise BackendError(friendly_error(error))

    async def copy_file(self, src_folder: int, message_id: int, dst_folder: int) -> int | None:
        client = self._client()
        src = await self._entity(src_folder)
        dst = await self._entity(dst_folder)
        group = db.group_by_message(src_folder, message_id)
        try:
            if group:
                parts = db.group_parts(src_folder, group["first_message_id"])
                ids = [p["message_id"] for p in parts]
                result = await client.forward_messages(dst, ids, src)
                messages = result if isinstance(result, list) else [result]
                new_ids = [m.id for m in messages if m is not None]
                if len(new_ids) != len(ids):
                    raise BackendError("复制失败")
                db.group_save(
                    dst_folder, new_ids[0], group["name"], group["size"],
                    group["mime"], group["kind"],
                    [{"id": new_id, "size": part["part_size"]}
                     for new_id, part in zip(new_ids, parts)],
                )
                override = db.display_names(
                    src_folder, [group["first_message_id"]]
                ).get(group["first_message_id"])
                if override:
                    db.set_display_name(dst_folder, new_ids[0], override)
                return new_ids[0]
            forwarded = await client.forward_messages(dst, message_id, src)
            new_id = forwarded_id(forwarded)
            override = db.display_names(src_folder, [message_id]).get(message_id)
            if new_id and override:
                db.set_display_name(dst_folder, new_id, override)
            return new_id
        except Exception as error:
            raise BackendError(friendly_error(error))

    async def rename_file(self, folder_id: int, message_id: int, new_name: str) -> dict[str, Any]:
        """Rename is a local overlay: Telegram has no rename, and re-uploading
        the bytes just to change a label is wasteful and slow."""
        new_name = os.path.basename(new_name.strip())
        if not new_name:
            raise BackendError("文件名不能为空")
        group = db.group_by_message(folder_id, message_id)
        target = group["first_message_id"] if group else message_id
        await self._message(folder_id, target)
        db.set_display_name(folder_id, target, new_name)
        return await self.file_info(folder_id, target)

    async def upload(self, folder_id: int, path: Path, name: str,
                     progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        client = self._client()
        entity = await self._entity(folder_id)
        size = os.path.getsize(path)
        part_bytes = max(1, config.TELEGRAM_PART_BYTES)
        if size <= part_bytes:
            try:
                uploaded = await client.upload_file(
                    str(path), part_size_kb=512, file_size=size, file_name=name,
                    progress_callback=(lambda cur, total: progress(cur, total) if progress else None),
                )
                message = await client.send_file(
                    entity, uploaded, force_document=True,
                    attributes=[DocumentAttributeFilename(file_name=name)],
                    caption="",
                )
            except Exception as error:
                raise BackendError(friendly_error(error))
            return describe(message)

        # Telegram caps one document at 2GB: send consecutive parts and keep a
        # local group row so the web app still shows a single file.
        count = (size + part_bytes - 1) // part_bytes
        mime, _ = mimetypes.guess_type(name)
        mime = mime or "application/octet-stream"
        kind = _kind_from_mime(mime)
        parts: list[dict[str, int]] = []
        try:
            with path.open("rb") as handle:
                for index in range(count):
                    offset = index * part_bytes
                    length = min(part_bytes, size - offset)
                    handle.seek(offset)
                    part_name = f"{name}.part{index + 1:03d}"
                    reader = _SliceReader(handle, length, part_name)

                    def report(current: int, _total: int, base: int = offset) -> None:
                        if progress:
                            progress(base + current, size)

                    uploaded = await client.upload_file(
                        reader, part_size_kb=512, file_size=length, file_name=part_name,
                        progress_callback=report,
                    )
                    caption = f"[TD-PART {index + 1}/{count}]"
                    message = await client.send_file(
                        entity, uploaded, force_document=True,
                        attributes=[DocumentAttributeFilename(file_name=part_name)],
                        caption=caption,
                    )
                    parts.append({"id": message.id, "size": length})
        except Exception as error:
            sent = [part["id"] for part in parts]
            if sent:
                try:
                    await client.delete_messages(entity, sent)
                except Exception:
                    pass
            raise BackendError(friendly_error(error))

        first = parts[0]["id"]
        db.group_save(folder_id, first, name, size, mime, kind, parts)
        info = describe(await self._message(folder_id, first))
        info.update({"name": name, "size": size, "mime": mime, "kind": kind, "parts": count})
        return info

    # ---- upload queue ----------------------------------------------------

    async def enqueue_upload(self, job: UploadJob) -> UploadJob:
        if self._upload_queue is None:
            self._upload_queue = asyncio.Queue(maxsize=64)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._upload_worker())
        self.jobs[job.id] = job
        await self._upload_queue.put(job)
        return job

    async def _upload_worker(self) -> None:
        assert self._upload_queue is not None
        while True:
            job: UploadJob = await self._upload_queue.get()
            path = config.TMP_DIR / job.id
            attempts = 0
            try:
                while True:
                    try:
                        job.state = "running"
                        db.job_save(job)

                        def report(sent: int, total: int) -> None:
                            job.sent, job.total = sent, total

                        info = await self.upload(job.folder_id, path, job.name, report)
                        job.message_id = info["id"]
                        job.sent = job.total or job.sent
                        job.state = "done"
                        db.job_save(job)
                        logger.info("upload done: %s -> message %s", job.name, job.message_id)
                        break
                    except asyncio.CancelledError:
                        job.state = "error"
                        job.error = "任务被中断"
                        db.job_save(job)
                        raise
                    except Exception as error:
                        attempts += 1
                        message = error.message if isinstance(error, BackendError) else friendly_error(error)
                        logger.warning("upload attempt %s failed for %s: %s", attempts, job.name, message)
                        retryable = attempts < 3 and "超过" not in message and "上限" not in message
                        if not retryable:
                            job.state = "error"
                            job.error = message
                            db.job_save(job)
                            break
                        await asyncio.sleep(min(5 * attempts, 15))
            finally:
                # Keep the temp file for failed jobs so /api/jobs/{id}/retry works.
                if job.state == "done":
                    try:
                        path.unlink()
                    except OSError:
                        pass
                self._upload_queue.task_done()


backend = TelegramBackend()
