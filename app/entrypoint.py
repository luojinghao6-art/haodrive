"""Container entrypoint: fix bind-mount ownership, then drop to the app user."""
import os
import sys

APP_UID = 10001
APP_GID = 10001
DATA_DIR = os.environ.get("DATA_DIR", "/data")


def _prepare_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.geteuid() != 0:
        return
    # Only the top level and the known subdirectories: a recursive chown over a
    # multi-gigabyte media cache on every start would be wasteful.
    targets = [DATA_DIR]
    for name in ("tmp", "cache", "cache/media"):
        path = os.path.join(DATA_DIR, name)
        os.makedirs(path, exist_ok=True)
        targets.append(path)
    for name in ("haodrive.db", "haodrive.db-wal", "haodrive.db-shm", "secret.key"):
        path = os.path.join(DATA_DIR, name)
        if os.path.exists(path):
            targets.append(path)
    for path in targets:
        try:
            os.chown(path, APP_UID, APP_GID)
        except OSError:
            pass


def main() -> None:
    _prepare_data_dir()
    if os.geteuid() == 0:
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    os.execvp(sys.executable, [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", "8000",
        "--proxy-headers", "--forwarded-allow-ips", "*",
        "--no-server-header",
    ])


if __name__ == "__main__":
    main()
