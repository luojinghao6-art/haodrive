"""Runtime configuration for seagulldrive."""
import os
import secrets
import stat
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR = DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DOMAIN = os.getenv("DOMAIN", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
DEMO_MODE = os.getenv("DEMO_MODE", "0").lower() in ("1", "true", "yes", "on")
PROXY = os.getenv("PROXY", "").strip()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024**3)))
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "7"))
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
# Chunk used for Telegram media range reads (must stay >= 4096).
STREAM_CHUNK = 512 * 1024
# Local disk cache for media blocks (video seeking, replays).
MEDIA_CACHE_GB = float(os.getenv("MEDIA_CACHE_GB", "4"))
WEBDAV_USER = os.getenv("WEBDAV_USER", "seagulldrive")
# Chunk size advertised to the web uploader.
UPLOAD_CHUNK = int(os.getenv("UPLOAD_CHUNK", str(8 * 1024 * 1024)))


def _persisted_secret() -> str:
    path = DATA_DIR / "secret.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return value


if not APP_SECRET:
    APP_SECRET = _persisted_secret()

if not ADMIN_PASSWORD and not ADMIN_PASSWORD_HASH:
    if DEMO_MODE:
        ADMIN_PASSWORD = "demo"
    else:
        raise SystemExit(
            "ADMIN_PASSWORD is not set. Set it in .env (the web login password)."
        )

if not DEMO_MODE and ADMIN_PASSWORD in ("change-me-to-a-long-password", "change-me-now", "password"):
    raise SystemExit("ADMIN_PASSWORD is still the shipped placeholder. Set a real password.")

if not DEMO_MODE and ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 10:
    raise SystemExit(
        "ADMIN_PASSWORD must be at least 10 characters. Generate one with: "
        "python -c \"import secrets; print(secrets.token_urlsafe(18))\""
    )

if not DOMAIN:
    DOMAIN = "localhost"

SECRET_PATH = os.getenv("SECRET_PATH", "").strip()
if not DEMO_MODE and SECRET_PATH:
    if len(SECRET_PATH) < 16 or SECRET_PATH in ("change-me-to-a-random-path", "change-me"):
        raise SystemExit(
            "SECRET_PATH must be at least 16 random characters. Generate one with: "
            "python -c \"import secrets,string; print(''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(24)))\""
        )
if not APP_SECRET or len(APP_SECRET) < 16:
    raise SystemExit("APP_SECRET must be at least 16 characters (or leave it empty to auto-generate).")
