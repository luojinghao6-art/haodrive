"""Runtime settings stored in SQLite.

Environment variables stay the source of defaults; anything the admin edits in
the web UI is written to the ``settings`` table and applied on top.  Because the
rest of the app reads ``config.X`` directly, applying a value simply rewrites the
attribute on the :mod:`app.config` module -- no call site has to change, and the
new value is live for the very next request.

A few settings need a side effect (the media cache is built once at startup),
so callers can subscribe with :func:`on_change`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, db

# Bumped when a stored value's meaning changes, so old rows are ignored.
DB_PREFIX = "cfg:"


@dataclass(frozen=True)
class Spec:
    key: str
    attr: str                 # attribute on app.config
    kind: str                 # str | int | float | bytes | bool
    group: str                # general | storage | transfer | media | security
    label_zh: str
    label_en: str
    hint_zh: str = ""
    hint_en: str = ""
    minimum: float | None = None
    maximum: float | None = None
    live: bool = True         # False -> takes effect after a restart
    secret: bool = False


SPECS: tuple[Spec, ...] = (
    Spec("site_name", "SITE_NAME", "str", "general", "站点名称", "Site name",
         "显示在标题栏和登录页", "Shown in the title bar and on the login page",
         minimum=1, maximum=40),
    Spec("max_upload_bytes", "MAX_UPLOAD_BYTES", "bytes", "storage", "单文件上限", "Max file size",
         "单个文件合并后的最大尺寸", "Largest file after parts are merged",
         minimum=1024 ** 2, maximum=1024 ** 4),
    Spec("storage_quota_bytes", "STORAGE_QUOTA_BYTES", "bytes", "storage", "显示配额", "Display quota",
         "只影响概览里的进度条，0 表示不显示", "Dashboard bar only; 0 hides it",
         minimum=0, maximum=1024 ** 4),
    Spec("telegram_part_bytes", "TELEGRAM_PART_BYTES", "bytes", "transfer", "分片大小", "Part size",
         "Telegram 单条消息上限 2GB，留一点余量", "Telegram caps one message at 2GB; keep some headroom",
         minimum=1024 ** 2, maximum=2_000_000_000),
    Spec("tmp_budget_bytes", "TMP_BUDGET_BYTES", "bytes", "transfer", "临时空间预算", "Temp budget",
         "服务端同时落盘的临时文件上限", "Ceiling for parts spooled to disk at once",
         minimum=1024 ** 2, maximum=200 * 1024 ** 3),
    Spec("upload_chunk", "UPLOAD_CHUNK", "bytes", "transfer", "上传分块", "Upload chunk",
         "浏览器每次 POST 的块大小", "Bytes per browser POST",
         minimum=256 * 1024, maximum=64 * 1024 ** 2),
    Spec("upload_concurrency", "UPLOAD_CONCURRENCY", "int", "transfer", "上传并发", "Upload concurrency",
         "同时发往 Telegram 的 512KB 分块数，越大越快，也越容易触发限流",
         "512KB parts in flight to Telegram; higher is faster but may hit FloodWait",
         minimum=1, maximum=16),
    Spec("upload_rate_kbps", "UPLOAD_RATE_KBPS", "int", "transfer", "上传限速", "Upload rate limit",
         "KB/s，0 表示不限速", "KB/s; 0 means unlimited",
         minimum=0, maximum=1024 * 1024),
    Spec("session_days", "SESSION_DAYS", "int", "security", "登录有效期", "Session lifetime",
         "天；只影响新登录", "Days; applies to new logins",
         minimum=1, maximum=365),
    Spec("media_cache_gb", "MEDIA_CACHE_GB", "float", "media", "媒体缓存", "Media cache",
         "GB，本地缓存视频块用于拖动", "GB of local blocks kept for seeking",
         minimum=0, maximum=1024),
    Spec("prebuffer_mb", "PREBUFFER_MB", "int", "media", "预缓冲", "Prebuffer",
         "MB，播放开始时提前缓存的量", "MB cached ahead when playback starts",
         minimum=0, maximum=1024),
    Spec("webdav_user", "WEBDAV_USER", "str", "security", "WebDAV 用户名", "WebDAV user",
         "rclone 等客户端登录用", "Used by rclone and friends",
         minimum=1, maximum=64),
)

BY_KEY: dict[str, Spec] = {spec.key: spec for spec in SPECS}

# Captured once, before any DB value is applied: the "reset to default" target.
DEFAULTS: dict[str, Any] = {spec.key: getattr(config, spec.attr) for spec in SPECS}

_listeners: dict[str, list[Callable[[Any], None]]] = {}


def on_change(key: str, callback: Callable[[Any], None]) -> None:
    _listeners.setdefault(key, []).append(callback)


def _coerce(spec: Spec, raw: Any) -> Any:
    """Turn a JSON value (or a stored string) into the type config expects."""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if spec.kind in ("int", "bytes"):
        value = int(float(raw))
    elif spec.kind == "float":
        value = float(raw)
    else:
        value = str(raw).strip()
        if not value:
            raise ValueError("不能为空")
        if len(value) > (spec.maximum or 10 ** 9):
            raise ValueError("太长")
        return value
    low = spec.minimum if spec.minimum is not None else 0
    high = spec.maximum if spec.maximum is not None else float("inf")
    if value < low or value > high:
        raise ValueError("超出范围")
    return value


def _notify(key: str, value: Any) -> None:
    for callback in _listeners.get(key, []):
        try:
            callback(value)
        except Exception:  # a broken listener must not block the save
            import logging
            logging.getLogger("haodrive").exception("settings listener failed: %s", key)


def apply_all() -> None:
    """Load every stored override on top of the environment defaults."""
    for spec in SPECS:
        stored = db.kv_get(DB_PREFIX + spec.key)
        if stored is None:
            setattr(config, spec.attr, DEFAULTS[spec.key])
            continue
        try:
            value = _coerce(spec, stored)
        except (TypeError, ValueError):
            continue  # corrupt row: keep the default
        setattr(config, spec.attr, value)
        _notify(spec.key, value)


def current() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for spec in SPECS:
        values[spec.key] = getattr(config, spec.attr)
    return values


def save(values: dict[str, Any]) -> list[str]:
    """Validate a batch of settings; returns a list of error messages.

    All keys are validated before anything is written, so a bad value never
    leaves the store half-updated.
    """
    errors: list[str] = []
    parsed: dict[str, Any] = {}
    for key, raw in values.items():
        spec = BY_KEY.get(key)
        if spec is None:
            errors.append(f"未知设置项：{key}")
            continue
        try:
            parsed[key] = _coerce(spec, raw)
        except (TypeError, ValueError) as error:
            errors.append(f"{spec.label_zh}：{error}")
    if errors:
        return errors

    # Cross-field sanity: a part can never be larger than the budget that
    # holds it, or every upload would be refused.
    merged = {**current(), **parsed}
    if merged["tmp_budget_bytes"] < merged["telegram_part_bytes"]:
        errors.append("临时空间预算不能小于分片大小")
    if merged["max_upload_bytes"] < merged["telegram_part_bytes"]:
        errors.append("单文件上限不能小于分片大小")
    if errors:
        return errors

    for key, value in parsed.items():
        spec = BY_KEY[key]
        db.kv_set(DB_PREFIX + key, str(value))
        setattr(config, spec.attr, value)
        _notify(key, value)
    return []


def reset(key: str) -> None:
    spec = BY_KEY.get(key)
    if spec is None:
        return
    db.kv_set(DB_PREFIX + key, None)
    value = DEFAULTS[key]
    setattr(config, spec.attr, value)
    _notify(key, value)


def describe() -> list[dict[str, Any]]:
    """Everything the settings page needs, without leaking anything secret."""
    out: list[dict[str, Any]] = []
    for spec in SPECS:
        out.append({
            "key": spec.key,
            "kind": spec.kind,
            "group": spec.group,
            "label": spec.label_zh,
            "label_en": spec.label_en,
            "hint": spec.hint_zh,
            "hint_en": spec.hint_en,
            "min": spec.minimum,
            "max": spec.maximum,
            "live": spec.live,
            "value": getattr(config, spec.attr),
            "default": DEFAULTS[spec.key],
        })
    return out


def set_admin_password(current_password: str, new_password: str) -> str | None:
    """Rotate the admin password. Returns an error string, or None on success."""
    from . import security

    if config.DEMO_MODE:
        return "演示模式不能改密码"
    if not security.check_password(current_password):
        return "当前密码不对"
    if len(new_password) < 10:
        return "新密码至少 10 位"
    if new_password == current_password:
        return "新密码和旧密码一样"
    db.kv_set("admin_password_hash", security.hash_password(new_password), secret=True)
    security.bump_session_epoch()
    return None
