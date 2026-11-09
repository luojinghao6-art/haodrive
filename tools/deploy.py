"""Ship the current working tree's runtime files to the VPS and restart.

Usage:  SSH_PASS=... .venv/Scripts/python.exe tools/deploy.py
"""
import io
import os
import sys
import tarfile
import time

import paramiko

HOST = os.environ.get("SSH_HOST", "104.160.37.83")
USER = os.environ.get("SSH_USER", "root")
PASSWORD = os.environ["SSH_PASS"]
ROOT = "/opt/haodrive"

FILES = [
    "app/settings.py",
    "app/main.py",
    "app/config.py",
    "app/security.py",
    "app/media_cache.py",
    "app/static/index.html",
    "app/static/style.css",
    "app/static/app.js",
    "app/static/i18n.js",
    "README.md",
    "README.en.md",
]


def main() -> int:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in FILES:
            tar.add(name, arcname=name)
    payload = buf.getvalue()
    print(f"bundle {len(payload) / 1024:.1f} KB, {len(FILES)} files")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30, banner_timeout=40)

    sftp = client.open_sftp()
    remote_tar = "/tmp/haodrive-update.tar.gz"
    try:
        with sftp.file(remote_tar, "wb") as handle:
            handle.write(payload)
        print("uploaded via sftp")
    except Exception as error:
        print(f"sftp failed ({error}); falling back to stdin")
        stdin, stdout, stderr = client.exec_command(f"cat > {remote_tar}")
        stdin.write(payload)
        stdin.channel.shutdown_write()
        stdout.channel.recv_exit_status()
    sftp.close()

    cmd = (
        f"cd {ROOT} && tar xzf {remote_tar} && "
        "rm -rf app/__pycache__ && "
        "chown -R haodrive:haodrive app README.md README.en.md && "
        "systemctl restart haodrive && sleep 3 && "
        "systemctl is-active haodrive && curl -s -o /dev/null -w 'local:%{http_code}\\n' http://127.0.0.1:8000/healthz"
    )
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    print(out.strip())
    if err.strip():
        print("STDERR:", err.strip())
    print("exit", code)

    time.sleep(2)
    stdin, stdout, stderr = client.exec_command(
        "journalctl -u haodrive -n 12 --no-pager | tail -12", timeout=30
    )
    print("--- recent log ---")
    print(stdout.read().decode(errors="replace").strip())
    client.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
