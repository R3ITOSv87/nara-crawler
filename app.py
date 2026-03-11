import json
import os
import random
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from queue import Queue

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
PAUSE_MIN = float(os.environ.get("PAUSE_MIN", "1.0"))
PAUSE_MAX = float(os.environ.get("PAUSE_MAX", "2.5"))
WORKERS = int(os.environ.get("WORKERS", "4"))
URLS_FILE = Path(__file__).parent / "urls.json"
LOCAL_TMP = Path("/tmp/nara-downloads")

# --- State ---
state = {
    "status": "starting",
    "total": 0,
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "bytes_total": 0,
    "current_files": [],   # list of currently active downloads
    "current_size": 0,
    "started_at": None,
    "last_download_at": None,
    "speed_bps": 0,
    "consecutive_errors": 0,
    "workers": WORKERS,
    "history": [],
    "errors": [],
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


def remote_file_exists(sftp, path):
    try:
        sftp.stat(path)
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


def download_one(item, worker_id):
    """Download a single PDF: S3 -> local temp -> SFTP upload -> cleanup."""
    filename = item["filename"]
    pdf_url = item["pdf_url"]
    remote_path = f"pdfs/{filename}"
    local_path = LOCAL_TMP / filename

    # Register as active
    with state_lock:
        state["current_files"].append(filename)

    try:
        # Get own SFTP connection (or reuse via thread-local)
        tl = _thread_local()
        sftp = tl.sftp

        # Check if already on storage box
        if remote_file_exists(sftp, remote_path):
            with state_lock:
                state["skipped"] += 1
            return "skipped"

        # Phase 1: Download from S3 to local temp file
        t0 = time.time()
        downloaded_bytes = 0
        with httpx.stream("GET", pdf_url, timeout=300, follow_redirects=True) as resp:
            resp.raise_for_status()
            content_length = int(resp.headers.get("content-length", 0))
            with state_lock:
                state["current_size"] = content_length

            with open(local_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=2097152):  # 2MB chunks
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

        # Phase 2: Upload local file to Storage Box via SFTP
        sftp.put(str(local_path), remote_path)

        # Cleanup local file
        local_path.unlink(missing_ok=True)

        elapsed = time.time() - t0
        speed = downloaded_bytes / elapsed if elapsed > 0 else 0

        with state_lock:
            state["downloaded"] += 1
            state["bytes_total"] += downloaded_bytes
            state["last_download_at"] = datetime.now(timezone.utc).isoformat()
            state["speed_bps"] = speed
            state["consecutive_errors"] = 0

        return "ok"

    except Exception as e:
        # Cleanup on failure
        local_path.unlink(missing_ok=True)
        error_msg = f"{filename}: {type(e).__name__}: {str(e)[:200]}"

        with state_lock:
            state["failed"] += 1
            state["consecutive_errors"] += 1
            state["errors"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": filename,
                "error": str(e)[:300],
            })
            state["errors"] = state["errors"][-50:]

            if state["consecutive_errors"] >= 5:
                send_pushover(f"5+ Fehler in Folge! Letzter: {error_msg}", priority=1)

        # Try to reconnect SFTP for this thread
        try:
            tl = _thread_local()
            tl.ssh.close()
        except Exception:
            pass
        try:
            tl = _thread_local(force_reconnect=True)
        except Exception:
            pass

        return "error"

    finally:
        with state_lock:
            if filename in state["current_files"]:
                state["current_files"].remove(filename)


# Thread-local SFTP connections
_tls = threading.local()


def _thread_local(force_reconnect=False):
    if force_reconnect or not hasattr(_tls, "ssh") or _tls.ssh is None:
        try:
            _tls.ssh.close()
        except Exception:
            pass
        ssh, sftp = get_sftp()
        _tls.ssh = ssh
        _tls.sftp = sftp
    return _tls


def crawler_thread():
    with open(URLS_FILE) as f:
        data = json.load(f)

    items = data["items"]

    with state_lock:
        state["total"] = len(items)
        state["status"] = "connecting"
        state["started_at"] = datetime.now(timezone.utc).isoformat()

    send_pushover(
        f"Crawler gestartet (v2 parallel).\n"
        f"{len(items)} PDFs, {WORKERS} Worker."
    )

    # Test SFTP connection
    try:
        ssh, sftp = get_sftp()
        try:
            sftp.stat("pdfs")
        except FileNotFoundError:
            sftp.mkdir("pdfs")
        sftp.close()
        ssh.close()
    except Exception as e:
        with state_lock:
            state["status"] = f"sftp_error: {e}"
        send_pushover(f"SFTP-Verbindung fehlgeschlagen: {e}", priority=1)
        return

    # Create local temp dir
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)

    with state_lock:
        state["status"] = "running"

    hour_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    hour_downloads = 0
    hour_errors = 0
    hour_bytes = 0
    milestone_next = 500
    last_activity = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {}

        for item in items:
            # Submit with jittered delay between submissions
            pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
            time.sleep(pause / WORKERS)  # Spread the jitter across workers

            future = executor.submit(download_one, item, 0)
            futures[future] = item

            # Process completed futures as they finish
            done_futures = [f for f in futures if f.done()]
            for f in done_futures:
                result = f.result()
                item_done = futures.pop(f)

                if result == "ok":
                    hour_downloads += 1
                    hour_bytes += 0  # tracked in state already
                    last_activity = time.time()
                elif result == "error":
                    hour_errors += 1

                # Hourly stats
                current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
                if current_hour != hour_bucket:
                    with state_lock:
                        state["history"].append({
                            "ts": hour_bucket,
                            "downloaded": hour_downloads,
                            "failed": hour_errors,
                            "bytes": hour_bytes,
                        })
                        state["history"] = state["history"][-168:]
                    hour_bucket = current_hour
                    hour_downloads = 0
                    hour_errors = 0
                    hour_bytes = 0

                # Milestones
                with state_lock:
                    done = state["downloaded"] + state["skipped"]
                if done >= milestone_next:
                    elapsed_s = time.time() - time.mktime(
                        datetime.fromisoformat(state["started_at"]).timetuple()
                    )
                    rate = done / elapsed_s if elapsed_s > 0 else 0
                    remaining = (state["total"] - done) / rate if rate > 0 else 0
                    send_pushover(
                        f"{done}/{state['total']} abgeschlossen\n"
                        f"{format_bytes(state['bytes_total'])}, "
                        f"ETA: {format_duration(remaining)}"
                    )
                    milestone_next += 500

                # Watchdog
                if time.time() - last_activity > 900:
                    send_pushover("Kein Download seit 15 Minuten!", priority=1)
                    last_activity = time.time()

        # Wait for remaining futures
        for f in as_completed(futures):
            result = f.result()
            if result == "ok":
                hour_downloads += 1
                last_activity = time.time()
            elif result == "error":
                hour_errors += 1

    # Final hourly bucket
    with state_lock:
        if hour_downloads or hour_errors:
            state["history"].append({
                "ts": hour_bucket,
                "downloaded": hour_downloads,
                "failed": hour_errors,
                "bytes": hour_bytes,
            })

    # Done
    with state_lock:
        state["status"] = "completed"
        state["current_files"] = []

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
        from flask import request
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
        s["current_files"] = list(state["current_files"])
    return jsonify(s)


# Start crawler in background thread
crawler = threading.Thread(target=crawler_thread, daemon=True)
crawler.start()
