"""Tiny server-side translations for pages that are rendered without the SPA.

Only the share pages need this: the main interface is translated in the
browser by app/static/i18n.js.
"""
from __future__ import annotations

from typing import Any

TEXTS: dict[str, tuple[str, str]] = {
    "need_password": ("需要密码", "Password required"),
    "link_missing": ("链接不存在", "Link not found"),
    "link_expired": ("链接已过期", "Link expired"),
    "file_deleted": ("文件已删除", "File deleted"),
    "file_missing": ("文件不存在", "File not found"),
    "password_protected": ("此链接受密码保护", "This link is password protected"),
    "access_password": ("访问密码", "Access password"),
    "unlock": ("解锁", "Unlock"),
    "download": ("下载", "Download"),
    "shared_folder": ("共享文件夹", "Shared folder"),
    "empty_folder": ("空文件夹", "Empty folder"),
    "file": ("文件", "File"),
    "folder": ("文件夹", "Folder"),
    "size_mb": ("{value} MB", "{value} MB"),
    "size_kb": ("{value} KB", "{value} KB"),
}


def detect(request: Any) -> str:
    """Return 'zh' or 'en'. Explicit ?lang= wins, then cookie, then header."""
    query = getattr(request, "query_params", None)
    if query is not None:
        value = (query.get("lang") or "").lower()
        if value.startswith("zh"):
            return "zh"
        if value.startswith("en"):
            return "en"
    cookie = getattr(request, "cookies", {}) or {}
    value = (cookie.get("hd_lang") or "").lower()
    if value.startswith("zh"):
        return "zh"
    if value.startswith("en"):
        return "en"
    header = (getattr(request, "headers", {}) or {}).get("accept-language", "").lower()
    if header:
        first = header.split(",")[0].split(";")[0].strip()
        if first.startswith("zh"):
            return "zh"
        if first.startswith("en"):
            return "en"
    return "zh"


def t(lang: str, key: str, **values: Any) -> str:
    zh, en = TEXTS.get(key, (key, key))
    text = en if lang == "en" else zh
    try:
        return text.format(**values)
    except (KeyError, IndexError):
        return text
