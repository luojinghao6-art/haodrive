"""SQLite storage: settings (encrypted), folder ordering, share links."""
import base64
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config

_DB_PATH = config.DATA_DIR / "haodrive.db"


def _migrate_legacy_name() -> None:
    """Move a pre-rename database (and its WAL sidecars) into place.

    Runs before the first connection, so a fresh install never creates an
    empty haodrive.db ahead of an existing seagulldrive.db.
    """
    legacy = config.DATA_DIR / "seagulldrive.db"
    if _DB_PATH.exists() or not legacy.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(legacy) + suffix)
        target = Path(str(_DB_PATH) + suffix)
        if source.exists() and not target.exists():
            source.replace(target)


_migrate_legacy_name()
_lock = threading.Lock()
fernet = Fernet(
    base64.urlsafe_b64encode(hashlib.sha256(("settings:" + config.APP_SECRET).encode()).digest())
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    secret INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS folders (
    channel_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT,
    is_public INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY,
    folder_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    password_hash TEXT,
    downloads INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS file_names (
    folder_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (folder_id, message_id)
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    folder_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_groups (
    folder_id INTEGER NOT NULL,
    first_message_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT,
    kind TEXT,
    created INTEGER NOT NULL,
    parts_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (folder_id, first_message_id)
);
CREATE TABLE IF NOT EXISTS file_group_parts (
    folder_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    first_message_id INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    part_size INTEGER NOT NULL,
    PRIMARY KEY (folder_id, message_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder_id INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    error TEXT,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    group_key TEXT,
    part_index INTEGER,
    part_count INTEGER
);
CREATE TABLE IF NOT EXISTS upload_groups (
    group_key TEXT PRIMARY KEY,
    folder_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    part_bytes INTEGER NOT NULL,
    part_count INTEGER NOT NULL,
    mime TEXT,
    kind TEXT,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_group_parts (
    group_key TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    part_size INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    message_id INTEGER,
    updated INTEGER NOT NULL,
    PRIMARY KEY (group_key, part_index)
);
CREATE TABLE IF NOT EXISTS traffic_daily (
    day TEXT PRIMARY KEY,
    up_bytes INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    up_files INTEGER NOT NULL DEFAULT 0,
    down_files INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS folder_storage (
    folder_id INTEGER PRIMARY KEY,
    files INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, ddl in (
            ("folder_id", "INTEGER NOT NULL DEFAULT 0"),
            ("group_key", "TEXT"),
            ("part_index", "INTEGER"),
            ("part_count", "INTEGER"),
        ):
            if name not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        upload_columns = {row["name"] for row in conn.execute("PRAGMA table_info(uploads)").fetchall()}
        for name, ddl in (("group_key", "TEXT"), ("part_index", "INTEGER"),
                          ("stream", "INTEGER NOT NULL DEFAULT 0"),
                          ("file_id", "INTEGER"),
                          ("confirmed", "INTEGER NOT NULL DEFAULT 0")):
            if name not in upload_columns:
                conn.execute(f"ALTER TABLE uploads ADD COLUMN {name} {ddl}")
    _DB_PATH.chmod(0o600)


def kv_get(key: str, default: str | None = None, secret: bool = False) -> str | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT value, secret FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    value = row["value"]
    if row["secret"] or secret:
        try:
            value = fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return default
    return value


def kv_set(key: str, value: str | None, secret: bool = False) -> None:
    with _lock, connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
            return
        stored = fernet.encrypt(value.encode()).decode() if secret else value
        conn.execute(
            "INSERT INTO settings(key, value, secret) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret=excluded.secret",
            (key, stored, 1 if secret else 0),
        )


# ---- folders -------------------------------------------------------------

def folder_rows() -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute("SELECT * FROM folders ORDER BY display_order, name").fetchall()
    return [dict(r) for r in rows]


def upsert_folder(channel_id: int, name: str, username: str | None, is_public: bool,
                  display_order: int | None = None) -> None:
    with _lock, connect() as conn:
        if display_order is None:
            row = conn.execute("SELECT MAX(display_order) FROM folders").fetchone()
            display_order = (row[0] or 0) + 1
        conn.execute(
            "INSERT INTO folders(channel_id, name, username, is_public, display_order, added_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
            "name=excluded.name, username=excluded.username, is_public=excluded.is_public",
            (channel_id, name, username, 1 if is_public else 0, display_order, int(time.time())),
        )


def remove_folder(channel_id: int) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM folders WHERE channel_id=?", (channel_id,))


def set_folder_order(order: list[int]) -> None:
    with _lock, connect() as conn:
        for index, channel_id in enumerate(order):
            conn.execute("UPDATE folders SET display_order=? WHERE channel_id=?", (index, channel_id))


# ---- shares --------------------------------------------------------------

def create_share(token: str, folder_id: int, message_id: int, expires_at: int | None,
                 password_hash: str | None) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO shares(token, folder_id, message_id, created_at, expires_at, password_hash) "
            "VALUES(?,?,?,?,?,?)",
            (token, folder_id, message_id, int(time.time()), expires_at, password_hash),
        )


def get_share(token: str) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def bump_share_downloads(token: str) -> None:
    with _lock, connect() as conn:
        conn.execute("UPDATE shares SET downloads=downloads+1 WHERE token=?", (token,))


def delete_share(token: str) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM shares WHERE token=?", (token,))


def list_shares() -> list[dict[str, Any]]:
    # Never expose password_hash to the API layer.
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT token, folder_id, message_id, created_at, expires_at, downloads "
            "FROM shares ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---- display names -------------------------------------------------------
# Telegram has no rename operation. We keep a local overlay: the file stays
# exactly as uploaded, only the name this web app shows changes.

def set_display_name(folder_id: int, message_id: int, name: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO file_names(folder_id, message_id, name, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(folder_id, message_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
            (folder_id, message_id, name, int(time.time())),
        )


def clear_display_name(folder_id: int, message_id: int) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM file_names WHERE folder_id=? AND message_id=?", (folder_id, message_id))


def display_names(folder_id: int, message_ids: list[int]) -> dict[int, str]:
    if not message_ids:
        return {}
    marks = ",".join("?" for _ in message_ids)
    with _lock, connect() as conn:
        rows = conn.execute(
            f"SELECT message_id, name FROM file_names WHERE folder_id=? AND message_id IN ({marks})",
            (folder_id, *message_ids),
        ).fetchall()
    return {row["message_id"]: row["name"] for row in rows}


def search_display_names(folder_id: int, query: str, limit: int = 200) -> list[int]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT message_id FROM file_names WHERE folder_id=? AND name LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (folder_id, f"%{query}%", limit),
        ).fetchall()
    return [row["message_id"] for row in rows]


def move_display_name(src_folder: int, src_message: int, dst_folder: int, dst_message: int) -> None:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT name FROM file_names WHERE folder_id=? AND message_id=?",
            (src_folder, src_message),
        ).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM file_names WHERE folder_id=? AND message_id=?", (src_folder, src_message))
        conn.execute(
            "INSERT INTO file_names(folder_id, message_id, name, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(folder_id, message_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
            (dst_folder, dst_message, row["name"], int(time.time())),
        )


# ---- upload sessions (survive restarts) ---------------------------------

def upload_save(upload_id: str, folder_id: int, name: str, size: int, received: int,
                created: int, group_key: str | None = None,
                part_index: int | None = None, stream: int = 0,
                file_id: int | None = None, confirmed: int = 0) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO uploads(id, folder_id, name, size, received, created, group_key, "
            "part_index, stream, file_id, confirmed) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET received=excluded.received, "
            "group_key=excluded.group_key, part_index=excluded.part_index, "
            "confirmed=excluded.confirmed, file_id=excluded.file_id, stream=excluded.stream",
            (upload_id, folder_id, name, size, received, created, group_key, part_index,
             stream, file_id, confirmed),
        )


def upload_set_confirmed(upload_id: str, confirmed: int) -> None:
    """Persist how far the streaming pump has confirmed bytes to Telegram."""
    with _lock, connect() as conn:
        conn.execute("UPDATE uploads SET confirmed=? WHERE id=?",
                     (int(confirmed), upload_id))


def upload_get(upload_id: str) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    return dict(row) if row else None


def upload_load_all() -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute("SELECT * FROM uploads").fetchall()
    return [dict(r) for r in rows]


def upload_delete(upload_id: str) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM uploads WHERE id=?", (upload_id,))


def upload_delete_group(group_key: str) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM uploads WHERE group_key=?", (group_key,))


# ---- transfer jobs -------------------------------------------------------

def job_save(job) -> None:
    now = int(time.time())
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id, name, folder_id, state, sent, total, message_id, error,"
            " created, updated, group_key, part_index, part_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "state=excluded.state, sent=excluded.sent, total=excluded.total, "
            "message_id=excluded.message_id, error=excluded.error, updated=excluded.updated, "
            "group_key=excluded.group_key, part_index=excluded.part_index, "
            "part_count=excluded.part_count",
            (job.id, job.name, job.folder_id, job.state, job.sent, job.total,
             job.message_id, job.error, now, now, getattr(job, "group_key", None),
             getattr(job, "part_index", None), getattr(job, "part_count", None)),
        )


def job_get(job_id: str) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def job_list(limit: int = 100) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def job_claim_retry(job_id: str) -> bool:
    with _lock, connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET state='queued', error=NULL, updated=? WHERE id=? AND state='error'",
            (int(time.time()), job_id),
        )
        return cur.rowcount == 1


def job_find_part(group_key: str, part_index: int) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE group_key=? AND part_index=? "
            "AND state IN ('queued','running') ORDER BY created DESC LIMIT 1",
            (group_key, part_index),
        ).fetchone()
    return dict(row) if row else None


def job_unfinished() -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE state IN ('queued','running')").fetchall()
    return [dict(r) for r in rows]


def job_prune(max_age: int = 86400, keep_terminal: int = 200) -> None:
    cutoff = int(time.time()) - max_age
    with _lock, connect() as conn:
        conn.execute("DELETE FROM jobs WHERE created < ? AND state IN ('done','error')", (cutoff,))
        conn.execute(
            "DELETE FROM jobs WHERE state IN ('done','error') AND id NOT IN "
            "(SELECT id FROM jobs WHERE state IN ('done','error') ORDER BY created DESC LIMIT ?)",
            (keep_terminal,),
        )


# ---- multi-part files ----------------------------------------------------
# Telegram caps a document at 2GB. Bigger uploads are sent as consecutive
# part messages; these tables stitch them into one logical file.

def _group_save_conn(conn: sqlite3.Connection, folder_id: int, first_id: int, name: str,
                     size: int, mime: str | None, kind: str | None,
                     parts: list[dict[str, int]]) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO file_groups(folder_id, first_message_id, name, size, mime, kind,"
        " created, parts_count) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(folder_id, first_message_id) DO UPDATE SET name=excluded.name,"
        " size=excluded.size, mime=excluded.mime, kind=excluded.kind,"
        " parts_count=excluded.parts_count",
        (folder_id, first_id, name, size, mime or "", kind or "file", now, len(parts)),
    )
    conn.execute(
        "DELETE FROM file_group_parts WHERE folder_id=? AND first_message_id=?",
        (folder_id, first_id),
    )
    conn.executemany(
        "INSERT INTO file_group_parts(folder_id, message_id, first_message_id,"
        " part_index, part_size) VALUES(?,?,?,?,?)",
        [(folder_id, int(p["id"]), first_id, index, int(p["size"]))
         for index, p in enumerate(parts)],
    )


def group_save(folder_id: int, first_id: int, name: str, size: int,
               mime: str | None, kind: str | None,
               parts: list[dict[str, int]]) -> None:
    with _lock, connect() as conn:
        _group_save_conn(conn, folder_id, first_id, name, size, mime, kind, parts)


def group_get(folder_id: int, first_id: int) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM file_groups WHERE folder_id=? AND first_message_id=?",
            (folder_id, first_id),
        ).fetchone()
    return dict(row) if row else None


def group_by_message(folder_id: int, message_id: int) -> dict[str, Any] | None:
    """Return the group a message belongs to, whether it is the first part or not."""
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT g.* FROM file_group_parts p JOIN file_groups g"
            " ON g.folder_id=p.folder_id AND g.first_message_id=p.first_message_id"
            " WHERE p.folder_id=? AND p.message_id=?",
            (folder_id, message_id),
        ).fetchone()
    return dict(row) if row else None


def group_parts(folder_id: int, first_id: int) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT message_id, part_size FROM file_group_parts"
            " WHERE folder_id=? AND first_message_id=? ORDER BY part_index",
            (folder_id, first_id),
        ).fetchall()
    return [{"message_id": row["message_id"], "part_size": row["part_size"]} for row in rows]


def groups_for_folder(folder_id: int) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Groups keyed by their first message id, plus ids that must stay hidden."""
    with _lock, connect() as conn:
        groups = conn.execute(
            "SELECT * FROM file_groups WHERE folder_id=?", (folder_id,)
        ).fetchall()
        parts = conn.execute(
            "SELECT message_id, first_message_id FROM file_group_parts WHERE folder_id=?",
            (folder_id,),
        ).fetchall()
    mapping = {row["first_message_id"]: dict(row) for row in groups}
    hidden = {row["message_id"] for row in parts
              if row["message_id"] != row["first_message_id"]}
    return mapping, hidden


def group_delete(folder_id: int, first_id: int) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "DELETE FROM file_group_parts WHERE folder_id=? AND first_message_id=?",
            (folder_id, first_id),
        )
        conn.execute(
            "DELETE FROM file_groups WHERE folder_id=? AND first_message_id=?",
            (folder_id, first_id),
        )


def search_groups(folder_id: int, query: str, limit: int = 200) -> list[int]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT first_message_id FROM file_groups WHERE folder_id=? AND name LIKE ?"
            " ORDER BY created DESC LIMIT ?",
            (folder_id, f"%{query}%", limit),
        ).fetchall()
    return [row["first_message_id"] for row in rows]

# ---- pending (client-side) part uploads ----------------------------------
# The browser splits a large file and uploads one part at a time. Until every
# part arrives these rows keep the part messages out of the file listing.

def part_group_get(group_key: str) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM upload_groups WHERE group_key=?", (group_key,)).fetchone()
    return dict(row) if row else None


def part_group_create(group_key: str, folder_id: int, name: str, size: int,
                      part_bytes: int, part_count: int, mime: str | None,
                      kind: str | None, part_sizes: list[int]) -> dict[str, Any]:
    now = int(time.time())
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO upload_groups(group_key, folder_id, name, size, part_bytes,"
            " part_count, mime, kind, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_key) DO UPDATE SET updated=excluded.updated",
            (group_key, folder_id, name, size, part_bytes, part_count,
             mime or "", kind or "file", now, now),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO upload_group_parts(group_key, part_index, part_size,"
            " state, updated) VALUES(?,?,?,'pending',?)",
            [(group_key, index, part_sizes[index], now) for index in range(part_count)],
        )
    return part_group_get(group_key)  # type: ignore[return-value]


def part_group_parts(group_key: str) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM upload_group_parts WHERE group_key=? ORDER BY part_index",
            (group_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def part_group_set_state(group_key: str, part_index: int, state: str,
                         message_id: int | None = None) -> None:
    with _lock, connect() as conn:
        if message_id is None:
            conn.execute(
                "UPDATE upload_group_parts SET state=?, updated=? "
                "WHERE group_key=? AND part_index=?",
                (state, int(time.time()), group_key, part_index),
            )
        else:
            conn.execute(
                "UPDATE upload_group_parts SET state=?, message_id=?, updated=? "
                "WHERE group_key=? AND part_index=?",
                (state, message_id, int(time.time()), group_key, part_index),
            )


def part_group_upload_session(group_key: str, part_index: int) -> dict[str, Any] | None:
    """Find an interrupted part session so a browser can resume its offset."""
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT id, size, received FROM uploads WHERE group_key=? AND part_index=?",
            (group_key, part_index),
        ).fetchone()
    return dict(row) if row else None


def part_group_finalize(group_key: str) -> dict[str, Any] | None:
    """Atomically register the logical file once every part is done.

    Returns None when parts are still missing. For a one-part upload the
    pending row is simply cleared: Telegram already has the file under its
    real name, so no group overlay is needed.
    """
    with _lock, connect() as conn:
        group = conn.execute(
            "SELECT * FROM upload_groups WHERE group_key=?", (group_key,)
        ).fetchone()
        if not group:
            return None
        parts = conn.execute(
            "SELECT * FROM upload_group_parts WHERE group_key=? ORDER BY part_index",
            (group_key,),
        ).fetchall()
        if len(parts) != group["part_count"] or any(p["state"] != "done" for p in parts):
            return None
        if any(not p["message_id"] for p in parts):
            return None
        if group["part_count"] > 1:
            first = parts[0]["message_id"]
            ordered = [{"id": p["message_id"], "size": p["part_size"]} for p in parts]
            _group_save_conn(conn, group["folder_id"], first, group["name"],
                             group["size"], group["mime"], group["kind"], ordered)
        conn.execute("DELETE FROM upload_group_parts WHERE group_key=?", (group_key,))
        conn.execute("DELETE FROM upload_groups WHERE group_key=?", (group_key,))
    return {"folder_id": group["folder_id"], "name": group["name"],
            "size": group["size"], "parts": group["part_count"]}


def part_group_delete(group_key: str) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM upload_group_parts WHERE group_key=?", (group_key,))
        conn.execute("DELETE FROM upload_groups WHERE group_key=?", (group_key,))


def part_groups_unfinished() -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute("SELECT * FROM upload_groups").fetchall()
    return [dict(row) for row in rows]


def pending_part_message_ids(folder_id: int) -> set[int]:
    """Part messages already in Telegram but not yet a complete logical file."""
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT p.message_id FROM upload_group_parts p JOIN upload_groups g"
            " ON g.group_key=p.group_key WHERE g.folder_id=? AND p.state='done'"
            " AND p.message_id IS NOT NULL",
            (folder_id,),
        ).fetchall()
    return {row["message_id"] for row in rows}


# ---- traffic + storage counters -----------------------------------------

def traffic_add(day: str, up_bytes: int = 0, down_bytes: int = 0,
                up_files: int = 0, down_files: int = 0) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO traffic_daily(day, up_bytes, down_bytes, up_files, down_files)"
            " VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
            "up_bytes=up_bytes+excluded.up_bytes, down_bytes=down_bytes+excluded.down_bytes,"
            " up_files=up_files+excluded.up_files, down_files=down_files+excluded.down_files",
            (day, up_bytes, down_bytes, up_files, down_files),
        )


def traffic_days(days: int = 30) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM traffic_daily ORDER BY day DESC LIMIT ?", (max(1, days),)
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def traffic_totals() -> dict[str, int]:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(up_bytes),0) AS up, COALESCE(SUM(down_bytes),0) AS down,"
            " COALESCE(SUM(up_files),0) AS up_files, COALESCE(SUM(down_files),0) AS down_files"
            " FROM traffic_daily"
        ).fetchone()
    return {"up": row["up"], "down": row["down"],
            "up_files": row["up_files"], "down_files": row["down_files"]}


def _storage_add_conn(conn: sqlite3.Connection, folder_id: int, files: int, size: int) -> None:
    conn.execute(
        "INSERT INTO folder_storage(folder_id, files, bytes, updated) VALUES(?,?,?,?) "
        "ON CONFLICT(folder_id) DO UPDATE SET files=files+excluded.files,"
        " bytes=bytes+excluded.bytes, updated=excluded.updated",
        (folder_id, files, size, int(time.time())),
    )
    conn.execute(
        "UPDATE folder_storage SET files=MAX(files,0), bytes=MAX(bytes,0) WHERE folder_id=?",
        (folder_id,),
    )


def storage_add(folder_id: int, files: int, size: int) -> None:
    with _lock, connect() as conn:
        _storage_add_conn(conn, folder_id, files, size)


def storage_move(src_folder: int, dst_folder: int, files: int, size: int) -> None:
    with _lock, connect() as conn:
        _storage_add_conn(conn, src_folder, -files, -size)
        _storage_add_conn(conn, dst_folder, files, size)


def storage_remove_folder(folder_id: int) -> None:
    with _lock, connect() as conn:
        conn.execute("DELETE FROM folder_storage WHERE folder_id=?", (folder_id,))


def storage_rows() -> dict[int, dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute("SELECT * FROM folder_storage").fetchall()
    return {row["folder_id"]: dict(row) for row in rows}


def storage_replace_all(rows: list[tuple[int, int, int]]) -> None:
    """Reconcile after a full scan: (folder_id, files, bytes)."""
    with _lock, connect() as conn:
        conn.execute("DELETE FROM folder_storage")
        conn.executemany(
            "INSERT INTO folder_storage(folder_id, files, bytes, updated) VALUES(?,?,?,?)",
            [(folder_id, files, size, int(time.time())) for folder_id, files, size in rows],
        )
