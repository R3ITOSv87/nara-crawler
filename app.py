import json
import os
import random
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import httpx
import paramiko
from flask import Flask, Response, jsonify, render_template

# --- Config ---
STORAGEBOX_HOST = os.environ.get("STORAGEBOX_HOST", "u506918-sub2.your-storagebox.de")
STORAGEBOX_PORT = int(os.environ.get("STORAGEBOX_PORT", "23"))
STORAGEBOX_USER = os.environ.get("STORAGEBOX_USER", "u506918-sub2")
STORAGEBOX_PASS = os.environ.get("STORAGEBOX_PASS", "")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
PAUSE_MIN = float(os.environ.get("PAUSE_MIN", "2.0"))
PAUSE_MAX = float(os.environ.get("PAUSE_MAX", "4.0"))
DATA_DIR = Path("/data")
URLS_FILE = Path(__file__).parent / "urls.json"

# --- State ---
state = {
    "status": "starting",
    "total": 0,
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "bytes_total": 0,
    "current_file": "",
    "current_size": 0,
    "started_at": None,
    "last_download_at": None,
    "speed_bps": 0,
    "consecutive_errors": 0,
    "history": [],  # [{ts, downloaded, failed, bytes}] per hour
    "errors": [],   # last 50 errors
}
state_lock = threading.Lock()


def send_pushover(message, priority=0, title="NARA Crawler"):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return
    try:
        httpx.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "message": message,
            "title": title,
            "priority": priority,
        }, timeout=10)
    except Exception:
        pass


def get_sftp():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(STORAGEBOX_HOST, port=STORAGEBOX_PORT,
                username=STORAGEBOX_USER, password=STORAGEBOX_PASS, timeout=30)
    return ssh, ssh.open_sftp()


def remote_file_exists(sftp, path, expected_size=None):
    try:
        info = sftp.stat(path)
        if expected_size and info.st_size != expected_size:
            return False
        return True
    except FileNotFoundError:
        return False


def format_bytes(b):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours/24:.1f}d"


def crawler_thread():
    with open(URLS_FILE) as f:
        data = json.load(f)

    items = data["items"]

    with state_lock:
        state["total"] = len(items)
        state["status"] = "connecting"
        state["started_at"] = datetime.now(timezone.utc).isoformat()

    send_pushover(f"Crawler gestartet. {len(items)} PDFs zum Download.")

    # Connect to storage box
    try:
        ssh, sftp = get_sftp()
    except Exception as e:
        with state_lock:
            state["status"] = f"sftp_error: {e}"
        send_pushover(f"SFTP-Verbindung fehlgeschlagen: {e}", priority=1)
        return

    # Ensure base directory exists
    try:
        sftp.stat("pdfs")
    except FileNotFoundError:
        sftp.mkdir("pdfs")

    with state_lock:
        state["status"] = "running"

    hour_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    hour_downloads = 0
    hour_errors = 0
    hour_bytes = 0
    milestone_next = 500
    last_activity = time.time()

    for i, item in enumerate(items):
        filename = item["filename"]
        pdf_url = item["pdf_url"]
        remote_path = f"pdfs/{filename}"

        with state_lock:
            state["current_file"] = filename

        # Check if already downloaded
        try:
            if remote_file_exists(sftp, remote_path):
                with state_lock:
                    state["skipped"] += 1
                continue
        except Exception:
            # Reconnect SFTP if connection dropped
            try:
                ssh.close()
            except Exception:
                pass
            try:
                ssh, sftp = get_sftp()
            except Exception as e:
                with state_lock:
                    state["status"] = f"sftp_reconnect_failed: {e}"
                send_pushover(f"SFTP-Reconnect fehlgeschlagen: {e}", priority=1)
                return

        # Download PDF from S3
        try:
            t0 = time.time()
            with httpx.stream("GET", pdf_url, timeout=300, follow_redirects=True) as resp:
                resp.raise_for_status()
                content_length = int(resp.headers.get("content-length", 0))

                with state_lock:
                    state["current_size"] = content_length

                # Stream to SFTP
                with sftp.open(remote_path, "wb") as remote_file:
                    downloaded_bytes = 0
                    for chunk in resp.iter_bytes(chunk_size=1048576):  # 1MB chunks
                        remote_file.write(chunk)
                        downloaded_bytes += len(chunk)

            elapsed = time.time() - t0
            speed = downloaded_bytes / elapsed if elapsed > 0 else 0

            with state_lock:
                state["downloaded"] += 1
                state["bytes_total"] += downloaded_bytes
                state["last_download_at"] = datetime.now(timezone.utc).isoformat()
                state["speed_bps"] = speed
                state["consecutive_errors"] = 0

            hour_downloads += 1
            hour_bytes += downloaded_bytes
            last_activity = time.time()

        except Exception as e:
            error_msg = f"{filename}: {type(e).__name__}: {str(e)[:200]}"

            with state_lock:
                state["failed"] += 1
                state["consecutive_errors"] += 1
                state["errors"].append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "file": filename,
                    "error": str(e)[:300]
                })
                state["errors"] = state["errors"][-50:]  # Keep last 50

                if state["consecutive_errors"] >= 5:
                    send_pushover(
                        f"5+ Fehler in Folge! Letzter: {error_msg}",
                        priority=1
                    )

            hour_errors += 1

        # Track hourly stats
        current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        if current_hour != hour_bucket:
            with state_lock:
                state["history"].append({
                    "ts": hour_bucket,
                    "downloaded": hour_downloads,
                    "failed": hour_errors,
                    "bytes": hour_bytes,
                })
                state["history"] = state["history"][-168:]  # Keep 7 days
            hour_bucket = current_hour
            hour_downloads = 0
            hour_errors = 0
            hour_bytes = 0

        # Milestone alerts
        with state_lock:
            done = state["downloaded"] + state["skipped"]
        if done >= milestone_next:
            elapsed = time.time() - time.mktime(
                datetime.fromisoformat(state["started_at"]).timetuple()
            )
            rate = done / elapsed if elapsed > 0 else 0
            remaining = (state["total"] - done) / rate if rate > 0 else 0
            send_pushover(
                f"{done}/{state['total']} abgeschlossen\n"
                f"{format_bytes(state['bytes_total'])}, "
                f"ETA: {format_duration(remaining)}"
            )
            milestone_next += 500

        # Watchdog: no download for 15 min
        if time.time() - last_activity > 900:
            send_pushover("Kein Download seit 15 Minuten!", priority=1)
            last_activity = time.time()  # Reset to avoid spam

        # Jittered pause
        pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
        time.sleep(pause)

    # Done
    with state_lock:
        state["status"] = "completed"
        state["current_file"] = ""

    try:
        ssh.close()
    except Exception:
        pass

    send_pushover(
        f"Download abgeschlossen!\n"
        f"{state['downloaded']} heruntergeladen, {state['skipped']} uebersprungen, "
        f"{state['failed']} Fehler\n"
        f"Gesamt: {format_bytes(state['bytes_total'])}"
    )


# --- Flask App ---
app = Flask(__name__)


def check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = None
        from flask import request
        if request.authorization:
            auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="NARA Crawler"'}
            )
        return f(*args, **kwargs)
    return decorated


@app.route("/")
@auth_required
def index():
    return render_template("index.html")


@app.route("/api/status")
@auth_required
def api_status():
    with state_lock:
        s = dict(state)
        s["errors"] = list(state["errors"])
        s["history"] = list(state["history"])
    return jsonify(s)


# Start crawler in background thread
crawler = threading.Thread(target=crawler_thread, daemon=True)
crawler.start()
